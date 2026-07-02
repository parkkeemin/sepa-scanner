# -*- coding: utf-8 -*-
"""
SEPA 스캐너 v7.0 — GitHub Actions 자동 실행판
- KRX OPEN API로 코스피+코스닥 전 종목 SEPA 채점
- 결과를 results.json + SEPA_결과.xlsx 로 저장
- 인증키는 환경변수 KRX_AUTH_KEY 로 주입 (GitHub Secrets)
"""

import os
import json
import time
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone

# ===== 설정 =====
AUTH_KEY = os.environ.get("KRX_AUTH_KEY", "")
MIN_SCORE = 7
REQUIRED_DAYS = 260          # 52주 고저점 정확도를 위해 260 거래일
STOP_LOSS_PCT = 8.0
API_DELAY = 0.3              # KRX 부하 방지

BASE_URL = "https://data-dbg.krx.co.kr/svc/apis"
ENDPOINTS = {
    "kospi": f"{BASE_URL}/sto/stk_bydd_trd",
    "kosdaq": f"{BASE_URL}/sto/ksq_bydd_trd",
}
HEADERS = {"AUTH_KEY": AUTH_KEY, "Content-Type": "application/json"}

KST = timezone(timedelta(hours=9))


def fetch_daily(market, date_str):
    """단일 날짜 전 종목 일별매매정보"""
    resp = requests.get(
        ENDPOINTS[market], params={"basDd": date_str}, headers=HEADERS, timeout=30
    )
    data = resp.json()
    return data.get("OutBlock_1", [])


def find_recent_trading_day(max_back=10):
    """최근 유효 거래일 자동 탐색"""
    for i in range(1, max_back + 1):
        d = datetime.now(KST) - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        ds = d.strftime("%Y%m%d")
        try:
            rows = fetch_daily("kospi", ds)
            if rows:
                print(f"[OK] 기준 거래일: {ds} ({len(rows)}종목)")
                return ds
        except Exception as e:
            print(f"[SKIP] {ds}: {e}")
        time.sleep(API_DELAY)
    raise RuntimeError("최근 10일 내 유효 거래일 없음")


def build_cache(market, base_date, required_days):
    """과거 N거래일 캐시 {date: {isu_cd: row}}"""
    cache = {}
    date = datetime.strptime(base_date, "%Y%m%d")
    fetched, attempts = 0, 0
    while fetched < required_days and attempts < required_days * 2:
        ds = date.strftime("%Y%m%d")
        if date.weekday() < 5:
            try:
                rows = fetch_daily(market, ds)
                if rows:
                    cache[ds] = {r["ISU_CD"]: r for r in rows}
                    fetched += 1
                    if fetched % 30 == 0:
                        print(f"  [{market}] {fetched}/{required_days}일...")
                time.sleep(API_DELAY)
            except Exception:
                pass
        date -= timedelta(days=1)
        attempts += 1
    print(f"[OK] {market} 캐시 {fetched}거래일")
    return cache


def extract_series(cache, isu_cd):
    closes, volumes = [], []
    for ds in sorted(cache.keys()):
        row = cache[ds].get(isu_cd)
        if row:
            try:
                closes.append(float(row["TDD_CLSPRC"].replace(",", "")))
                volumes.append(float(row["ACC_TRDVOL"].replace(",", "")))
            except Exception:
                pass
    return closes, volumes


def ma(arr, n):
    return float(np.mean(arr[-n:])) if len(arr) >= n else None


def sepa_score(closes, volumes, stop_loss_pct=8.0):
    if len(closes) < 200:
        return 0, {}
    cur = closes[-1]
    ma50, ma150, ma200 = ma(closes, 50), ma(closes, 150), ma(closes, 200)
    ma200_prev = ma(closes[:-30], 200) if len(closes) >= 230 else None
    hi52 = max(closes[-252:]) if len(closes) >= 252 else max(closes)
    lo52 = min(closes[-252:]) if len(closes) >= 252 else min(closes)
    vol_recent = float(np.mean(volumes[-5:])) if len(volumes) >= 5 else None
    vol_ma50 = ma(volumes, 50)
    rr = (max(closes[-10:]) - min(closes[-10:])) / closes[-10] if len(closes) >= 10 else None
    pr = (max(closes[-30:-10]) - min(closes[-30:-10])) / closes[-30] if len(closes) >= 30 else None

    d = {
        "trend1_ma50": int(bool(ma50 and cur > ma50)),
        "trend2_ma150": int(bool(ma150 and cur > ma150)),
        "trend3_ma200": int(bool(ma200 and cur > ma200)),
        "trend4_cross": int(bool(ma150 and ma200 and ma150 > ma200)),
        "trend5_ma200_rising": int(bool(ma200 and ma200_prev and ma200 > ma200_prev)),
        "strength1_low52": int(cur >= lo52 * 1.30),
        "strength2_high52": int(cur >= hi52 * 0.75),
        "volume": int(bool(vol_recent and vol_ma50 and vol_recent > vol_ma50)),
        "vcp": int(bool(rr and pr and rr < pr)),
    }
    if ma50 and cur > 0:
        d["risk_stop"] = int((cur - ma50) / cur * 100 <= stop_loss_pct)
    else:
        d["risk_stop"] = 0
    return sum(d.values()), d


def grade(score):
    if score >= 9:
        return "최우선"
    if score == 8:
        return "집중관찰"
    if score == 7:
        return "관심종목"
    return "제외"


def scan(market, label, cache):
    results = []
    if not cache:
        return results
    latest = sorted(cache.keys())[-1]
    tickers = list(cache[latest].keys())
    print(f"[SCAN] {label} {len(tickers)}종목")
    for i, isu_cd in enumerate(tickers):
        try:
            closes, volumes = extract_series(cache, isu_cd)
            if len(closes) < 200:
                continue
            score, detail = sepa_score(closes, volumes, STOP_LOSS_PCT)
            if score >= MIN_SCORE:
                row = cache[latest][isu_cd]
                results.append({
                    "market": label,
                    "code": row["ISU_CD"],
                    "name": row["ISU_NM"],
                    "price": float(row["TDD_CLSPRC"].replace(",", "")),
                    "score": score,
                    "grade": grade(score),
                    "detail": detail,
                })
        except Exception:
            continue
        if (i + 1) % 300 == 0:
            print(f"  {i+1}/{len(tickers)}... (통과 {len(results)})")
    print(f"[OK] {label} 통과 {len(results)}종목")
    return results


def main():
    if not AUTH_KEY:
        raise RuntimeError("환경변수 KRX_AUTH_KEY 가 비어있습니다 (GitHub Secrets 확인)")

    base_date = find_recent_trading_day()

    all_results = []
    for market, label in [("kospi", "코스피"), ("kosdaq", "코스닥")]:
        cache = build_cache(market, base_date, REQUIRED_DAYS)
        all_results += scan(market, label, cache)

    all_results.sort(key=lambda x: (-x["score"], x["name"]))

    output = {
        "scan_date": base_date,
        "generated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
        "min_score": MIN_SCORE,
        "total": len(all_results),
        "counts": {
            "최우선": sum(1 for r in all_results if r["grade"] == "최우선"),
            "집중관찰": sum(1 for r in all_results if r["grade"] == "집중관찰"),
            "관심종목": sum(1 for r in all_results if r["grade"] == "관심종목"),
        },
        "results": all_results,
    }

    with open("results.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=1)
    print(f"[DONE] results.json 저장 — 총 {len(all_results)}종목")

    # 엑셀도 함께 저장 (다운로드용)
    if all_results:
        df = pd.DataFrame([
            {"시장": r["market"], "종목코드": r["code"], "종목명": r["name"],
             "현재가": r["price"], "SEPA점수": r["score"], "등급": r["grade"]}
            for r in all_results
        ])
        df.to_excel("SEPA_결과.xlsx", index=False)
        print("[DONE] SEPA_결과.xlsx 저장")


if __name__ == "__main__":
    main()
