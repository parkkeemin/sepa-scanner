#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_setup.py — 데일리 매매 기준 시스템 v1.0
------------------------------------------------------------------
김종봉(주도주 시작점) / 김정수(바닥권 턴어라운드) 2트랙 기계적 스크리너.

하는 일
  1) KRX OPEN API에서 코스피·코스닥 전종목 일별 시세를 받아 로컬 캐시에 누적
  2) 자체 산출 시장지수(전종목 시가총액 합)로 시장 국면 판정
  3) 트랙 A / 트랙 B 조건으로 종목 선별
  4) 종목별 매수(돌파/눌림)·1·2차 익절·손절가를 호가단위로 반올림해 계산
  5) 보유·관심 종목의 고점 위험 캔들 경고
  6) docs/data/daily.json 으로 출력 (대시보드가 읽음)

중요
  본 스크립트의 모든 가격은 규칙에 따른 '기계적 계산값'이며 예측이나
  투자 권유가 아닙니다. 최종 판단과 책임은 사용자에게 있습니다.

사용법
  export KRX_AUTH_KEY=xxxx
  python daily_setup.py                 # 최근 거래일 기준 실행
  python daily_setup.py --date 20260828 # 특정일 기준 실행
  python daily_setup.py --backfill 260  # 최초 1회 과거 데이터 채우기
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import gzip
import glob
import datetime as dt
from pathlib import Path

import requests
import pandas as pd

# ============================================================
# 1. 설정  (환경변수로 덮어쓸 수 있음)
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "data" / "ohlcv"
OUT_JSON = BASE_DIR / "docs" / "data" / "daily.json"
WATCHLIST = BASE_DIR / "watchlist.txt"          # 보유/관심 종목 (한 줄에 6자리 코드)
SECTOR_MAP = BASE_DIR / "data" / "sector_map.csv"

KRX_BASE = "https://data-dbg.krx.co.kr/svc/apis/"
KRX_ENDPOINTS = {
    "KOSPI": "sto/stk_bydd_trd",
    "KOSDAQ": "sto/ksq_bydd_trd",
}
AUTH_KEY = os.environ.get("KRX_AUTH_KEY", "")

def _f(name, default):
    return float(os.environ.get(name, default))

def _i(name, default):
    return int(os.environ.get(name, default))

# ---- 공통 ----
KEEP_DAYS       = _i("KEEP_DAYS", 300)      # 캐시 보관 거래일 수
MIN_PRICE       = _i("MIN_PRICE", 1000)     # 동전주 제외
MIN_MKTCAP      = _i("MIN_MKTCAP", 50_000_000_000)   # 시총 500억 미만 제외
MAX_STOP_PCT    = _f("MAX_STOP_PCT", 0.10)  # 손절폭 상한 (영상 기준 -10%)
MAX_BAR_RANGE   = _f("MAX_BAR_RANGE", 0.20) # 기준봉 고저 폭 상한 (변동성 과대 제외)
MIN_RR          = _f("MIN_RR", 1.0)         # 1차 목표 기준 최소 손익비
TARGET1_PCT     = _f("TARGET1_PCT", 0.10)   # 1차 익절 +10%
TARGET2_PCT     = _f("TARGET2_PCT", 0.20)   # 2차 익절 +20%
TRAIL_PCT       = _f("TRAIL_PCT", 0.07)     # 잔여 물량 추적손절 (고점 대비)

# ---- 트랙 A : 김종봉 · 주도주 시작점 ----
A_MIN_VALUE     = _f("A_MIN_VALUE", 200_000_000_000)  # 거래대금 2,000억
A_CHG_MIN       = _f("A_CHG_MIN", 5.0)      # 등락률 하한 %
A_CHG_MAX       = _f("A_CHG_MAX", 15.0)     # 상한 (상한가·과열 제외)
A_BODY_MIN      = _f("A_BODY_MIN", 0.03)    # (종가-시가)/시가 최소
A_BODY_RATIO    = _f("A_BODY_RATIO", 0.50)  # 실체/전체범위 최소
A_UPTAIL_MAX    = _f("A_UPTAIL_MAX", 0.35)  # 윗꼬리/전체범위 최대
A_VALUE_SURGE   = _f("A_VALUE_SURGE", 3.0)  # 20일 평균 거래대금 대비 배수
A_MAX_RUNUP     = _f("A_MAX_RUNUP", 3.0)    # 250일 저점 대비 3배 초과 상승 제외

# ---- 트랙 B : 김정수 · 바닥권 턴어라운드 ----
B_MIN_VALUE     = _f("B_MIN_VALUE", 10_000_000_000)   # 최소 거래대금 100억
B_POS_MAX       = _f("B_POS_MAX", 0.40)     # 52주 레인지 하위 40% 이내
B_BOX_MAX       = _f("B_BOX_MAX", 0.40)     # 직전 60일 박스권 폭 40% 이하
B_VOL_VS_PREV   = _f("B_VOL_VS_PREV", 3.0)  # 전일 대비 거래량 300%
B_VOL_VS_AVG60  = _f("B_VOL_VS_AVG60", 4.0) # 60일 평균 거래량 대비 배수
B_CHG_MIN       = _f("B_CHG_MIN", 5.0)
B_CHG_MAX       = _f("B_CHG_MAX", 25.0)
B_UPTAIL_MAX    = _f("B_UPTAIL_MAX", 0.35)

# ---- 고점 위험 캔들 ----
W_RUNUP         = _f("W_RUNUP", 2.0)        # 250일 저점 대비 2배 이상일 때만 경고
W_UPTAIL        = _f("W_UPTAIL", 0.50)      # 윗꼬리 50% 이상
W_DOJI_BODY     = _f("W_DOJI_BODY", 0.10)   # 실체 10% 이하 = 도지
W_GAP           = _f("W_GAP", 0.03)         # 갭상승 3% 이상 후 음봉

# 제외할 종목명 키워드 (우선주·스팩·리츠·ETN 등)
EXCLUDE_KEYWORDS = ("스팩", "리츠", "ETN", "홀딩스우", "우B", "우C")


# ============================================================
# 2. 호가단위 (2023-01-25 개정 기준 / 확인일 2026-08-30)
#    * 유가증권·코스닥 동일 적용
# ============================================================

TICK_TABLE = [
    (2_000,     1),
    (5_000,     5),
    (20_000,    10),
    (50_000,    50),
    (200_000,   100),
    (500_000,   500),
    (float("inf"), 1_000),
]

def tick_size(price: float) -> int:
    for upper, tick in TICK_TABLE:
        if price < upper:
            return tick
    return 1_000

def round_tick(price: float, mode: str = "near") -> int:
    """호가단위에 맞춰 정수 가격으로 반올림. mode: near / up / down"""
    if price is None or price != price or price <= 0:
        return 0
    t = tick_size(price)
    if mode == "up":
        return int(-(-price // t) * t)
    if mode == "down":
        return int(price // t * t)
    return int(round(price / t) * t)


# ============================================================
# 3. KRX 수집 · 캐시
# ============================================================

COLS = ["date", "code", "name", "market", "open", "high", "low",
        "close", "chg", "volume", "value", "mktcap"]

def krx_fetch_day(basdd: str, market: str, retries: int = 3) -> list[dict]:
    """KRX OPEN API 일별매매정보 1일치. 휴장일이면 빈 리스트."""
    if not AUTH_KEY:
        raise RuntimeError("KRX_AUTH_KEY 환경변수가 비어 있습니다.")
    url = KRX_BASE + KRX_ENDPOINTS[market]
    headers = {"AUTH_KEY": AUTH_KEY}
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, params={"basDd": basdd}, timeout=30)
            if r.status_code != 200:
                time.sleep(2 * (attempt + 1))
                continue
            block = r.json().get("OutBlock_1") or []
            rows = []
            for it in block:
                try:
                    close = int(str(it.get("TDD_CLSPRC", "0")).replace(",", "") or 0)
                    if close <= 0:
                        continue
                    rows.append({
                        "date":   basdd,
                        "code":   str(it.get("ISU_CD", "")).strip()[-6:],
                        "name":   str(it.get("ISU_NM", "")).strip(),
                        "market": market,
                        "open":   int(str(it.get("TDD_OPNPRC", "0")).replace(",", "") or 0),
                        "high":   int(str(it.get("TDD_HGPRC", "0")).replace(",", "") or 0),
                        "low":    int(str(it.get("TDD_LWPRC", "0")).replace(",", "") or 0),
                        "close":  close,
                        "chg":    float(str(it.get("FLUC_RT", "0")).replace(",", "") or 0),
                        "volume": int(str(it.get("ACC_TRDVOL", "0")).replace(",", "") or 0),
                        "value":  int(str(it.get("ACC_TRDVAL", "0")).replace(",", "") or 0),
                        "mktcap": int(str(it.get("MKTCAP", "0")).replace(",", "") or 0),
                    })
                except (TypeError, ValueError):
                    continue
            return rows
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
    print(f"  ! {market} {basdd} 수집 실패", file=sys.stderr)
    return []


def cache_path(basdd: str) -> Path:
    return CACHE_DIR / f"{basdd[:6]}.csv.gz"


def cached_dates() -> set[str]:
    dates: set[str] = set()
    for p in sorted(glob.glob(str(CACHE_DIR / "*.csv.gz"))):
        try:
            d = pd.read_csv(p, usecols=["date"], dtype={"date": str})
            dates |= set(d["date"].unique())
        except Exception:
            continue
    return dates


def save_day(basdd: str, rows: list[dict]) -> None:
    if not rows:
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = cache_path(basdd)
    new = pd.DataFrame(rows, columns=COLS)
    if p.exists():
        old = pd.read_csv(p, dtype={"date": str, "code": str})
        old = old[old["date"] != basdd]
        new = pd.concat([old, new], ignore_index=True)
    new.sort_values(["date", "code"]).to_csv(p, index=False, compression="gzip")


def collect(target: str, backfill: int) -> str:
    """target 기준일부터 과거로 backfill 거래일만큼 캐시를 채우고, 실제 최근 거래일 반환."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    have = cached_dates()
    d = dt.datetime.strptime(target, "%Y%m%d").date()
    got, tried, latest = 0, 0, None
    while got < backfill and tried < backfill * 2 + 40:
        tried += 1
        basdd = d.strftime("%Y%m%d")
        if d.weekday() >= 5:                     # 주말 건너뜀
            d -= dt.timedelta(days=1)
            continue
        if basdd in have:
            got += 1
            latest = latest or basdd
            d -= dt.timedelta(days=1)
            continue
        rows = krx_fetch_day(basdd, "KOSPI") + krx_fetch_day(basdd, "KOSDAQ")
        if rows:
            save_day(basdd, rows)
            got += 1
            latest = latest or basdd
            print(f"  + {basdd} {len(rows):,}종목")
        else:
            print(f"  . {basdd} 휴장 또는 데이터 없음")
        d -= dt.timedelta(days=1)
        time.sleep(0.2)
    return latest or target


def load_cache(limit_days: int = KEEP_DAYS) -> pd.DataFrame:
    files = sorted(glob.glob(str(CACHE_DIR / "*.csv.gz")))
    if not files:
        raise RuntimeError("캐시가 비어 있습니다. --backfill 260 으로 먼저 실행하세요.")
    df = pd.concat(
        [pd.read_csv(f, dtype={"date": str, "code": str}) for f in files],
        ignore_index=True,
    )
    keep = sorted(df["date"].unique())[-limit_days:]
    df = df[df["date"].isin(keep)]
    return df.sort_values(["code", "date"]).reset_index(drop=True)


# ============================================================
# 4. 시장 국면 (자체 산출 지수 = 전종목 시가총액 합)
# ============================================================

def market_regime(df: pd.DataFrame, market: str) -> dict:
    sub = df[df["market"] == market]
    idx = sub.groupby("date").agg(cap=("mktcap", "sum")).sort_index()
    if len(idx) < 20:
        return {"market": market, "state": "데이터부족", "note": "거래일 20일 미만"}
    idx["ma20"] = idx["cap"].rolling(20).mean()
    idx["ma60"] = idx["cap"].rolling(60).mean()
    last = idx.iloc[-1]
    prev = idx.iloc[-2]
    lo, hi = idx["cap"].min(), idx["cap"].max()
    pos = (last["cap"] - lo) / (hi - lo) if hi > lo else 0.5

    ma20 = last["ma20"]
    ma60 = last["ma60"] if pd.notna(last["ma60"]) else None

    if ma60 is not None and last["cap"] < ma60:
        state, action = "위험", "신규 진입 중단 · 현금 비중 확대"
    elif pd.notna(ma20) and last["cap"] < ma20:
        state, action = "주의", "신규 1종목까지만 · 비중 절반"
    elif ma60 is not None and ma20 > ma60:
        state, action = "정상", "기준 충족 시 정상 진입"
    else:
        state, action = "혼조", "기준 충족 종목만 소량"

    if pos > 0.90 and state in ("정상", "혼조"):
        state = state + "(과열구간)"
        action = "분할 진입 · 목표 도달 시 기계적 익절"

    day_chg = (last["cap"] / prev["cap"] - 1) * 100
    return {
        "market": market,
        "state": state,
        "action": action,
        "day_chg": round(float(day_chg), 2),
        "pos_pct": round(float(pos) * 100, 1),
        "vs_ma20": round(float(last["cap"] / ma20 - 1) * 100, 2) if pd.notna(ma20) else None,
        "vs_ma60": round(float(last["cap"] / ma60 - 1) * 100, 2) if ma60 else None,
    }


# ============================================================
# 5. 지표 계산
# ============================================================

def build_features(df: pd.DataFrame, today: str) -> pd.DataFrame:
    g = df.groupby("code", sort=False)
    df = df.copy()
    df["v_avg20"] = g["value"].transform(lambda s: s.shift(1).rolling(20, min_periods=10).mean())
    df["vol_avg60"] = g["volume"].transform(lambda s: s.shift(1).rolling(60, min_periods=30).mean())
    df["vol_prev"] = g["volume"].shift(1)
    df["hi250"] = g["high"].transform(lambda s: s.rolling(250, min_periods=60).max())
    df["lo250"] = g["low"].transform(lambda s: s.rolling(250, min_periods=60).min())
    df["box_hi60"] = g["close"].transform(lambda s: s.shift(1).rolling(60, min_periods=40).max())
    df["box_lo60"] = g["close"].transform(lambda s: s.shift(1).rolling(60, min_periods=40).min())
    df["ma20"] = g["close"].transform(lambda s: s.rolling(20, min_periods=15).mean())
    df["days"] = g.cumcount() + 1

    t = df[df["date"] == today].copy()
    rng = (t["high"] - t["low"]).replace(0, pd.NA)
    t["body_pct"] = (t["close"] - t["open"]) / t["open"] * 100
    t["body_ratio"] = (t["close"] - t["open"]).abs() / rng
    t["uptail_ratio"] = (t["high"] - t[["open", "close"]].max(axis=1)) / rng
    t["pos_52w"] = (t["close"] - t["lo250"]) / (t["hi250"] - t["lo250"]).replace(0, pd.NA)
    t["runup"] = t["close"] / t["lo250"]
    t["box_width"] = (t["box_hi60"] / t["box_lo60"]) - 1
    t["value_surge"] = t["value"] / t["v_avg20"]
    t["vol_vs_prev"] = t["volume"] / t["vol_prev"].replace(0, pd.NA)
    t["vol_vs_avg60"] = t["volume"] / t["vol_avg60"]
    return t


def is_tradable(row) -> bool:
    name = str(row["name"])
    if any(k in name for k in EXCLUDE_KEYWORDS):
        return False
    if name.endswith("우") or name.endswith("우B"):
        return False
    if row["close"] < MIN_PRICE:
        return False
    if row["mktcap"] < MIN_MKTCAP:
        return False
    if row["days"] < 60:
        return False
    return True


# ============================================================
# 6. 매매 계획 (기계적 계산값)
# ============================================================

def make_plan(row) -> dict | None:
    high, low, op, cl = row["high"], row["low"], row["open"], row["close"]
    buy_break = round_tick(high * 1.001, "up")           # 돌파 매수: 기준봉 고가 위
    buy_pull = round_tick(op + (cl - op) * 0.5, "near")  # 눌림 매수: 실체 50% 되돌림
    stop_raw = round_tick(low * 0.99, "down")            # 기준봉 저가 이탈
    floor = round_tick(buy_break * (1 - MAX_STOP_PCT), "up")   # -10% 초과 손절 금지
    stop = max(stop_raw, floor)
    if stop >= buy_break:
        return None
    risk = (buy_break - stop) / buy_break
    t1 = round_tick(buy_break * (1 + TARGET1_PCT), "near")
    t2 = round_tick(buy_break * (1 + TARGET2_PCT), "near")
    rr = (t1 - buy_break) / (buy_break - stop)
    bar_range = (high - low) / low if low > 0 else 9.99
    reasons = []
    if bar_range > MAX_BAR_RANGE:
        reasons.append(f"기준봉 고저 폭 {bar_range*100:.0f}% — 변동성 과대")
    if rr < MIN_RR:
        reasons.append(f"손익비 {rr:.2f} — 기준 미달")
    if risk > MAX_STOP_PCT + 1e-9:
        reasons.append("손절폭 상한 초과")
    return {
        "buy_break": buy_break,
        "buy_pull": buy_pull,
        "stop": stop,
        "stop_pct": round(-risk * 100, 2),
        "target1": t1,
        "target2": t2,
        "trail_pct": round(TRAIL_PCT * 100, 1),
        "rr": round(float(rr), 2),
        "bar_range_pct": round(float(bar_range) * 100, 1),
        "ok": not reasons,
        "reject": reasons,
    }


def load_sector_map() -> dict:
    if SECTOR_MAP.exists():
        try:
            m = pd.read_csv(SECTOR_MAP, dtype=str)
            return dict(zip(m["code"].str.zfill(6), m["sector"]))
        except Exception:
            pass
    return {}


# ============================================================
# 7. 스크리닝
# ============================================================

def screen_track_a(t: pd.DataFrame, sectors: dict) -> list[dict]:
    hot = t[(t["value"] >= A_MIN_VALUE) & (t["chg"] >= 3.0)]
    hot_sectors: dict[str, list[str]] = {}
    for _, r in hot.iterrows():
        s = sectors.get(r["code"])
        if s:
            hot_sectors.setdefault(s, []).append(r["name"])

    out = []
    for _, r in t.iterrows():
        if not is_tradable(r):
            continue
        checks = {
            "거래대금 2,000억 이상": bool(r["value"] >= A_MIN_VALUE),
            f"등락률 +{A_CHG_MIN:.0f}~+{A_CHG_MAX:.0f}%": bool(A_CHG_MIN <= r["chg"] <= A_CHG_MAX),
            "장대양봉 실체 확보": bool(r["body_pct"] >= A_BODY_MIN * 100 and r["body_ratio"] >= A_BODY_RATIO),
            "긴 윗꼬리 없음": bool(pd.notna(r["uptail_ratio"]) and r["uptail_ratio"] <= A_UPTAIL_MAX),
            f"거래대금 20일 평균 {A_VALUE_SURGE:.0f}배↑": bool(pd.notna(r["value_surge"]) and r["value_surge"] >= A_VALUE_SURGE),
            f"바닥 대비 {A_MAX_RUNUP:.0f}배 미만": bool(pd.notna(r["runup"]) and r["runup"] < A_MAX_RUNUP),
        }
        failed = [k for k, v in checks.items() if not v]
        if len(failed) > 1:
            continue
        plan = make_plan(r)
        if not plan:
            continue
        if plan["reject"]:
            failed = failed + plan["reject"]
        if len(failed) > 1:
            continue
        sec = sectors.get(r["code"])
        peers = [n for n in hot_sectors.get(sec, []) if n != r["name"]] if sec else []
        out.append({
            "track": "A",
            "status": "pass" if not failed else "near",
            "missing": failed,
            "code": r["code"], "name": r["name"], "market": r["market"],
            "close": int(r["close"]), "chg": round(float(r["chg"]), 2),
            "value_eok": int(r["value"] / 1e8),
            "value_surge": round(float(r["value_surge"]), 1),
            "pos_52w": round(float(r["pos_52w"]) * 100, 1) if pd.notna(r["pos_52w"]) else None,
            "runup": round(float(r["runup"]), 2) if pd.notna(r["runup"]) else None,
            "uptail": round(float(r["uptail_ratio"]) * 100, 1) if pd.notna(r["uptail_ratio"]) else None,
            "sector": sec, "peers": peers[:5],
            "sector_confirmed": (len(peers) >= 1) if sec else None,
            "checks": checks, "plan": plan,
            "bar": {"open": int(r["open"]), "high": int(r["high"]), "low": int(r["low"]), "close": int(r["close"])},
        })
    out.sort(key=lambda x: -x["value_eok"])
    return out


def screen_track_b(t: pd.DataFrame, sectors: dict) -> list[dict]:
    out = []
    for _, r in t.iterrows():
        if not is_tradable(r):
            continue
        checks = {
            "52주 하위 40% 바닥권": bool(pd.notna(r["pos_52w"]) and r["pos_52w"] <= B_POS_MAX),
            "60일 박스권 횡보": bool(pd.notna(r["box_width"]) and r["box_width"] <= B_BOX_MAX),
            "전일 대비 거래량 300%↑": bool(pd.notna(r["vol_vs_prev"]) and r["vol_vs_prev"] >= B_VOL_VS_PREV),
            f"60일 평균 거래량 {B_VOL_VS_AVG60:.0f}배↑": bool(pd.notna(r["vol_vs_avg60"]) and r["vol_vs_avg60"] >= B_VOL_VS_AVG60),
            f"등락률 +{B_CHG_MIN:.0f}~+{B_CHG_MAX:.0f}% 양봉": bool(B_CHG_MIN <= r["chg"] <= B_CHG_MAX and r["close"] > r["open"]),
            "긴 윗꼬리 없음": bool(pd.notna(r["uptail_ratio"]) and r["uptail_ratio"] <= B_UPTAIL_MAX),
            "거래대금 100억 이상": bool(r["value"] >= B_MIN_VALUE),
        }
        failed = [k for k, v in checks.items() if not v]
        if len(failed) > 1:
            continue
        plan = make_plan(r)
        if not plan:
            continue
        if plan["reject"]:
            failed = failed + plan["reject"]
        if len(failed) > 1:
            continue
        out.append({
            "track": "B",
            "status": "pass" if not failed else "near",
            "missing": failed,
            "code": r["code"], "name": r["name"], "market": r["market"],
            "close": int(r["close"]), "chg": round(float(r["chg"]), 2),
            "value_eok": int(r["value"] / 1e8),
            "vol_vs_prev": round(float(r["vol_vs_prev"]), 1),
            "vol_vs_avg60": round(float(r["vol_vs_avg60"]), 1),
            "pos_52w": round(float(r["pos_52w"]) * 100, 1),
            "box_width": round(float(r["box_width"]) * 100, 1),
            "uptail": round(float(r["uptail_ratio"]) * 100, 1),
            "sector": sectors.get(r["code"]), "peers": [],
            "sector_confirmed": None,
            "checks": checks, "plan": plan,
            "bar": {"open": int(r["open"]), "high": int(r["high"]), "low": int(r["low"]), "close": int(r["close"])},
        })
    out.sort(key=lambda x: -x["vol_vs_avg60"])
    return out


def screen_warnings(t: pd.DataFrame, codes: set[str]) -> list[dict]:
    """고점 위험 캔들 — 보유/관심 종목 + 그날 급등 대형주 대상."""
    if codes:
        sub = t[t["code"].isin(codes)]
    else:
        sub = t[(t["value"] >= A_MIN_VALUE) | (t["chg"].abs() >= 10)]
    out = []
    for _, r in sub.iterrows():
        if r["close"] < MIN_PRICE or pd.isna(r["runup"]):
            continue
        flags = []
        if r["runup"] >= W_RUNUP:
            if pd.notna(r["uptail_ratio"]) and r["uptail_ratio"] >= W_UPTAIL:
                flags.append("긴 윗꼬리")
            if pd.notna(r["body_ratio"]) and r["body_ratio"] <= W_DOJI_BODY:
                flags.append("도지형")
            if r["open"] > 0 and r["vol_prev"] and pd.notna(r["vol_prev"]):
                prev_close = r["close"] - (r["close"] * r["chg"] / (100 + r["chg"])) if r["chg"] != -100 else 0
                if prev_close > 0 and (r["open"] / prev_close - 1) >= W_GAP and r["close"] < r["open"]:
                    flags.append("갭상승 후 음봉")
        if flags:
            out.append({
                "code": r["code"], "name": r["name"],
                "close": int(r["close"]), "chg": round(float(r["chg"]), 2),
                "runup": round(float(r["runup"]), 2),
                "pos_52w": round(float(r["pos_52w"]) * 100, 1) if pd.notna(r["pos_52w"]) else None,
                "flags": flags,
            })
    out.sort(key=lambda x: -x["runup"])
    return out[:20]


# ============================================================
# 8. 실행
# ============================================================

def prune_cache() -> None:
    files = sorted(glob.glob(str(CACHE_DIR / "*.csv.gz")))
    keep_months = 16
    for f in files[:-keep_months]:
        os.remove(f)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.date.today().strftime("%Y%m%d"))
    ap.add_argument("--backfill", type=int, default=3,
                    help="채울 거래일 수 (최초 1회 260 권장)")
    ap.add_argument("--no-fetch", action="store_true", help="캐시만 사용")
    args = ap.parse_args()

    if not args.no_fetch:
        print(f"[1/4] KRX 수집 (기준 {args.date}, {args.backfill}거래일)")
        collect(args.date, args.backfill)
        prune_cache()

    print("[2/4] 캐시 로드")
    df = load_cache()
    today = sorted(df["date"].unique())[-1]
    print(f"      기준 거래일 {today} / 종목 {df[df['date'] == today].shape[0]:,}개")

    print("[3/4] 지표·스크리닝")
    t = build_features(df, today)
    sectors = load_sector_map()
    all_a = screen_track_a(t, sectors)
    all_b = screen_track_b(t, sectors)
    track_a = [x for x in all_a if x["status"] == "pass"]
    track_b = [x for x in all_b if x["status"] == "pass"]
    near = [x for x in all_a + all_b if x["status"] == "near"]

    watch: set[str] = set()
    if WATCHLIST.exists():
        watch = {
            ln.strip().split(",")[0].zfill(6)
            for ln in WATCHLIST.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")
        }
    warnings = screen_warnings(t, watch)

    payload = {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "base_date": f"{today[:4]}-{today[4:6]}-{today[6:]}",
        "universe": int(t.shape[0]),
        "regime": [market_regime(df, "KOSPI"), market_regime(df, "KOSDAQ")],
        "track_a": track_a,
        "track_b": track_b,
        "near": near,
        "warnings": warnings,
        "config": {
            "A_MIN_VALUE_eok": int(A_MIN_VALUE / 1e8),
            "A_CHG": [A_CHG_MIN, A_CHG_MAX],
            "B_POS_MAX": B_POS_MAX,
            "B_VOL_VS_PREV": B_VOL_VS_PREV,
            "TARGET1_PCT": TARGET1_PCT,
            "TARGET2_PCT": TARGET2_PCT,
            "MAX_STOP_PCT": MAX_STOP_PCT,
            "MIN_RR": MIN_RR,
            "sector_map": bool(sectors),
        },
    }

    print("[4/4] 저장")
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"      트랙A {len(track_a)}종목 / 트랙B {len(track_b)}종목 / "
          f"1개조건 미달 {len(near)}종목 / 경고 {len(warnings)}건")
    print(f"      -> {OUT_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
