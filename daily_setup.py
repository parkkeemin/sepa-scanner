#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_setup.py — 데일리 매매 기준 시스템 v2.0 (영상 원문 기준 복원)
------------------------------------------------------------------
김종봉(주도주 시작점) / 김정수(바닥권 턴어라운드) 2트랙 기계적 스크리너.

하는 일
  1) KRX OPEN API에서 코스피·코스닥 전종목 일별 시세를 받아 로컬 캐시에 누적
  2) 자체 산출 시장지수(전종목 시가총액 합)로 시장 국면을 '맥락'으로 표시
     — 진입을 막지 않는다. 지수가 빠지는데 조건을 채운 종목은 오히려 강한 종목
  3) 트랙 A / 트랙 B 조건으로 종목 선별
  4) 종목별 매수(돌파/눌림)·1·2차 익절·손절가를 호가단위로 반올림해 계산
  5) 보유·관심 종목의 고점 위험 캔들 경고
  6) data/daily.json 으로 출력 (대시보드가 읽음)

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
# GitHub Pages 를 레포 루트(main /)에서 배포하므로 결과 JSON 도 루트의 data/ 아래에 둡니다.
# daily.html 이 fetch("./data/daily.json") 로 읽습니다.
OUT_JSON = BASE_DIR / "data" / "daily.json"
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
#  "최소 한 5%에서 10% 정도 사이에 장대 양봉이 터져야 되고요.
#   거래 대금이 2,000억 이상이 나와야 돼요."
#  "두산에너빌리티는 예를 들기에 아쉬운 종목. 시가총액이 커서 2,000억이 너무 쉽게 터져요."
#  "주도주는 이미 여기까지 올라왔기 때문에 주도주. 개미들은 여기서 관심을 가져…
#   그 시작점에서 2,000억이 터졌을 때 제일 처음이 언제였지, 요렇게 보는 거예요."
A_MIN_VALUE     = _f("A_MIN_VALUE", 200_000_000_000)   # 거래대금 2,000억 (원문)
A_CHG_MIN       = _f("A_CHG_MIN", 5.0)      # 원문: 5%
A_CHG_MAX       = _f("A_CHG_MAX", 10.0)     # 원문: 10%
A_MAX_MKTCAP    = _f("A_MAX_MKTCAP", 10_000_000_000_000)  # 시총 10조 초과 제외
A_BODY_MIN      = _f("A_BODY_MIN", 0.03)    # 장대양봉 실체 (종가-시가)/시가
A_BODY_RATIO    = _f("A_BODY_RATIO", 0.50)  # 실체/전체범위
A_UPTAIL_MAX    = _f("A_UPTAIL_MAX", 0.35)  # 윗꼬리/전체범위
A_MAX_RUNUP     = _f("A_MAX_RUNUP", 3.0)    # 1년 저점 대비 3배 초과 제외
# '시작점'을 더 좁게 — 최근 저점을 찍고 갓 올라온 구간만 (사장님 요청, 2026-08-30)
RECENT_LOW_DAYS   = _i("RECENT_LOW_DAYS", 60)     # '최근 저점'을 몇 거래일에서 찾을지
A_MAX_RUNUP_RECENT = _f("A_MAX_RUNUP_RECENT", 1.20)  # 최근 저점 대비 +20% 미만

# ---- 트랙 B : 김정수 · 바닥권 턴어라운드 ----
#  "최고가 대비 10분의 1 이상 떨어진 상황에서 바닥 치고 도는 종목"
#  "7, 8개월을 그냥 바닥을 기면서"
#  "300만 주 이상의 거래량이 발생했고 전일 대비 300% 이상의 거래량이 발생한 그런 종목만"
B_FROM_HIGH_MAX = _f("B_FROM_HIGH_MAX", 0.35)   # 창내 고점 대비 35% 이하 (=65%↓ 하락)
B_BOX_DAYS      = _i("B_BOX_DAYS", 150)         # 횡보 관찰 기간 (약 7~8개월)
B_BOX_MAX       = _f("B_BOX_MAX", 0.40)         # 그 기간 종가 변동폭 상한
B_VOL_ABS       = _f("B_VOL_ABS", 3_000_000)    # 절대 거래량 300만 주 (원문)
B_VOL_VS_PREV   = _f("B_VOL_VS_PREV", 3.0)      # 전일 대비 300% (원문)
B_CHG_MIN       = _f("B_CHG_MIN", 5.0)          # 장대양봉 (원문에 상한 없음)
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
    day_chg = (last["cap"] / prev["cap"] - 1) * 100

    # 지수는 '진입 금지 스위치'가 아니라 맥락이다.
    #  "지수가 빠지는데 2,000억이 터지는 양봉이 나왔어요. 강한 종목이죠. 그 종목 하는 거예요."
    #  "지수가 계속 오를 때 수익 나는 건 당연한 이야기. 지수가 빠졌음에도 수익을 보고 있으면
    #   이 사람은 주식을 잘하는 거예요."
    if ma60 is not None and last["cap"] < ma60:
        state = "하락추세"
        note = "지수 60일선 아래. 이 구간에서 조건을 충족한 종목은 시장보다 강한 종목입니다"
    elif pd.notna(ma20) and last["cap"] < ma20:
        state = "조정"
        note = "지수 20일선 아래. 내 수익이 실력인지 지수 덕인지 가려지는 구간입니다"
    elif ma60 is not None and ma20 > ma60:
        state = "상승추세"
        note = "지수 상승 구간. 수익이 나도 내 실력인지 지수 덕인지 구분하십시오"
    else:
        state = "중립"
        note = "방향성 불분명"

    if pos > 0.90:
        state += "(고점권)"
        note = "1년 범위 상위 10%. 지수 고점 여부를 먼저 확인하라는 구간입니다"

    return {
        "market": market,
        "state": state,
        "action": note,
        "falling": bool(day_chg < 0),
        "day_chg": num(day_chg, 2),
        "pos_pct": num(pos * 100, 1),
        "vs_ma20": num((last["cap"] / ma20 - 1) * 100, 2) if pd.notna(ma20) else None,
        "vs_ma60": num((last["cap"] / ma60 - 1) * 100, 2) if ma60 else None,
    }


# ============================================================
# 5. 지표 계산
# ============================================================

def _safe_div(num, den):
    """0 으로 나눌 때 예외 대신 NaN. float dtype 을 유지해 pd.NA 오염을 막는다."""
    num = pd.to_numeric(num, errors="coerce").astype("float64")
    den = pd.to_numeric(den, errors="coerce").astype("float64")
    return num.div(den.where(den != 0))


def num(v, nd: int = 2):
    """NaN·NA 를 None 으로. JSON 에 NaN 토큰이 새어 나가면 대시보드가 파싱에 실패한다."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return int(round(f)) if nd == 0 else round(f, nd)


def ok(v) -> bool:
    """NaN·NA·None 을 안전하게 False 로 변환. (NA 를 bool() 하면 TypeError)"""
    try:
        if v is None or v is pd.NA:
            return False
        if isinstance(v, float) and (v != v):     # NaN
            return False
        return bool(v)
    except (TypeError, ValueError):
        return False


def build_features(df: pd.DataFrame, today: str) -> pd.DataFrame:
    df = df.copy()
    for c in ("open", "high", "low", "close", "volume", "value", "mktcap", "chg"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    g = df.groupby("code", sort=False)
    df["v_avg20"] = g["value"].transform(lambda s: s.shift(1).rolling(20, min_periods=10).mean())
    df["vol_avg60"] = g["volume"].transform(lambda s: s.shift(1).rolling(60, min_periods=30).mean())
    df["vol_prev"] = g["volume"].shift(1)
    df["hi250"] = g["high"].transform(lambda s: s.rolling(250, min_periods=60).max())
    df["lo250"] = g["low"].transform(lambda s: s.rolling(250, min_periods=60).min())
    _rl = max(20, RECENT_LOW_DAYS)
    df["lo_recent"] = g["low"].transform(
        lambda s: s.rolling(_rl, min_periods=int(_rl * 0.5)).min())
    _bd = max(60, B_BOX_DAYS)
    df["box_hi"] = g["close"].transform(
        lambda s: s.shift(1).rolling(_bd, min_periods=int(_bd * 0.6)).max())
    df["box_lo"] = g["close"].transform(
        lambda s: s.shift(1).rolling(_bd, min_periods=int(_bd * 0.6)).min())
    df["ma20"] = g["close"].transform(lambda s: s.rolling(20, min_periods=15).mean())
    df["days"] = g.cumcount() + 1

    t = df[df["date"] == today].copy()
    rng = t["high"] - t["low"]
    # 봉 자체가 성립하지 않는 행(거래정지 등: 시가·고가·저가가 0) 은 지표를 계산하지 않는다
    t["valid_bar"] = (t["open"] > 0) & (t["low"] > 0) & (t["high"] >= t["low"])
    t["body_pct"] = _safe_div(t["close"] - t["open"], t["open"]) * 100
    t["body_ratio"] = _safe_div((t["close"] - t["open"]).abs(), rng)
    t["uptail_ratio"] = _safe_div(t["high"] - t[["open", "close"]].max(axis=1), rng)
    t["pos_52w"] = _safe_div(t["close"] - t["lo250"], t["hi250"] - t["lo250"])
    t["from_high"] = _safe_div(t["close"], t["hi250"])      # 창내 고점 대비 현재 위치
    t["runup"] = _safe_div(t["close"], t["lo250"])
    t["runup_recent"] = _safe_div(t["close"], t["lo_recent"])   # 최근 저점 대비 상승 배수
    t["box_width"] = _safe_div(t["box_hi"], t["box_lo"]) - 1
    t["value_surge"] = _safe_div(t["value"], t["v_avg20"])  # 참고 표시용 (선별 조건 아님)
    t["vol_vs_prev"] = _safe_div(t["volume"], t["vol_prev"])
    t["vol_vs_avg60"] = _safe_div(t["volume"], t["vol_avg60"])  # 참고 표시용
    return t


def is_tradable(row) -> bool:
    name = str(row.get("name", ""))
    if any(k in name for k in EXCLUDE_KEYWORDS):
        return False
    if name.endswith("우") or name.endswith("우B"):
        return False
    if "valid_bar" in row and not ok(row["valid_bar"]):   # 거래정지 등 봉 미성립
        return False
    if not ok(row["close"] >= MIN_PRICE):
        return False
    if not ok(row["mktcap"] >= MIN_MKTCAP):
        return False
    if not ok(row["days"] >= 60):
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

def screen_track_a(t: pd.DataFrame, sectors: dict, falling: dict | None = None) -> list[dict]:
    """김종봉 · 주도주 시작점."""
    falling = falling or {}
    # 같은 날 2,000억 이상 터진 양봉 종목 목록 — 섹터 매핑이 없어도
    # "두산이 터지고 한전산업도 터지고… 찾아보니 다 원전주였다"를 눈으로 확인할 수 있게 한다.
    hot = t[(t["value"] >= A_MIN_VALUE) & (t["chg"] >= A_CHG_MIN)]
    hot_names = list(hot["name"])
    hot_sectors: dict[str, list[str]] = {}
    for _, r in hot.iterrows():
        s = sectors.get(r["code"])
        if s:
            hot_sectors.setdefault(s, []).append(r["name"])

    out = []
    for _, r in t.iterrows():
      try:
        if not is_tradable(r):
            continue
        checks = {
            f"거래대금 {A_MIN_VALUE/1e8:,.0f}억 이상": ok(r["value"] >= A_MIN_VALUE),
            f"등락률 +{A_CHG_MIN:.0f}~+{A_CHG_MAX:.0f}% 장대양봉": ok(A_CHG_MIN <= r["chg"] <= A_CHG_MAX),
            "양봉 실체 확보": ok(r["body_pct"] >= A_BODY_MIN * 100 and r["body_ratio"] >= A_BODY_RATIO),
            "긴 윗꼬리 없음": ok(pd.notna(r["uptail_ratio"]) and r["uptail_ratio"] <= A_UPTAIL_MAX),
            f"시총 {A_MAX_MKTCAP/1e12:,.0f}조 이하": ok(r["mktcap"] <= A_MAX_MKTCAP),
            f"저점 대비 {A_MAX_RUNUP:.0f}배 미만": ok(pd.notna(r["runup"]) and r["runup"] < A_MAX_RUNUP),
            f"최근 {RECENT_LOW_DAYS}일 저점 대비 +{(A_MAX_RUNUP_RECENT-1)*100:.0f}% 미만 (매수 초입)": ok(
                pd.notna(r["runup_recent"]) and r["runup_recent"] < A_MAX_RUNUP_RECENT),
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
        peers = ([n for n in hot_sectors.get(sec, []) if n != r["name"]] if sec
                 else [n for n in hot_names if n != r["name"]])
        out.append({
            "track": "A",
            "status": "pass" if not failed else "near",
            "missing": failed,
            "strong_in_weak": bool(falling.get(r["market"])),
            "code": r["code"], "name": r["name"], "market": r["market"],
            "close": int(r["close"]), "chg": round(float(r["chg"]), 2),
            "value_eok": num(r["value"] / 1e8, 0),
            "value_surge": num(r["value_surge"], 1),
            "mktcap_jo": num(r["mktcap"] / 1e12, 2),
            "pos_52w": num(r["pos_52w"] * 100, 1),
            "runup": num(r["runup"], 2),
            "up_from_low": num((r["runup_recent"] - 1) * 100, 1),
            "uptail": num(r["uptail_ratio"] * 100, 1),
            "sector": sec, "peers": peers[:6],
            "sector_confirmed": (len(peers) >= 1) if peers else False,
            "checks": checks, "plan": plan,
            "bar": {"open": int(r["open"]), "high": int(r["high"]), "low": int(r["low"]), "close": int(r["close"])},
        })
      except Exception as e:      # 한 종목의 이상 데이터가 전체 스캔을 죽이지 않게
        print(f"  ! 트랙A 판정 건너뜀 {r.get('code', '?')} {r.get('name', '?')}: "
              f"{type(e).__name__} {e}", file=sys.stderr)
        continue
    out.sort(key=lambda x: -(x["value_eok"] or 0))
    return out


def screen_track_b(t: pd.DataFrame, sectors: dict, falling: dict | None = None) -> list[dict]:
    """김정수 · 바닥권 턴어라운드."""
    falling = falling or {}
    out = []
    for _, r in t.iterrows():
      try:
        if not is_tradable(r):
            continue
        checks = {
            f"고점 대비 {(1-B_FROM_HIGH_MAX)*100:.0f}%↓ 바닥권": ok(
                pd.notna(r["from_high"]) and r["from_high"] <= B_FROM_HIGH_MAX),
            f"{B_BOX_DAYS}일 박스권 횡보": ok(pd.notna(r["box_width"]) and r["box_width"] <= B_BOX_MAX),
            f"거래량 {B_VOL_ABS/1e4:,.0f}만 주 이상": ok(r["volume"] >= B_VOL_ABS),
            "전일 대비 거래량 300%↑": ok(pd.notna(r["vol_vs_prev"]) and r["vol_vs_prev"] >= B_VOL_VS_PREV),
            f"장대양봉 +{B_CHG_MIN:.0f}%↑": ok(r["chg"] >= B_CHG_MIN and r["close"] > r["open"]),
            "긴 윗꼬리 없음": ok(pd.notna(r["uptail_ratio"]) and r["uptail_ratio"] <= B_UPTAIL_MAX),
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
            "strong_in_weak": bool(falling.get(r["market"])),
            "code": r["code"], "name": r["name"], "market": r["market"],
            "close": int(r["close"]), "chg": round(float(r["chg"]), 2),
            "value_eok": num(r["value"] / 1e8, 0),
            "vol_vs_prev": num(r["vol_vs_prev"], 1),
            "vol_vs_avg60": num(r["vol_vs_avg60"], 1),
            "vol_man": num(r["volume"] / 1e4, 0),
            "from_high_pct": num((1 - r["from_high"]) * 100, 1),
            "pos_52w": num(r["pos_52w"] * 100, 1),
            "up_from_low": num((r["runup_recent"] - 1) * 100, 1),
            "box_width": num(r["box_width"] * 100, 1),
            "uptail": num(r["uptail_ratio"] * 100, 1),
            "sector": sectors.get(r["code"]), "peers": [],
            "sector_confirmed": None,
            "checks": checks, "plan": plan,
            "bar": {"open": int(r["open"]), "high": int(r["high"]), "low": int(r["low"]), "close": int(r["close"])},
        })
      except Exception as e:
        print(f"  ! 트랙B 판정 건너뜀 {r.get('code', '?')} {r.get('name', '?')}: "
              f"{type(e).__name__} {e}", file=sys.stderr)
        continue
    out.sort(key=lambda x: -(x["vol_vs_avg60"] or 0))
    return out


def screen_warnings(t: pd.DataFrame, codes: set[str]) -> list[dict]:
    """고점 위험 캔들 — 보유/관심 종목 + 그날 급등 대형주 대상."""
    if codes:
        sub = t[t["code"].isin(codes)]
    else:
        sub = t[(t["value"] >= A_MIN_VALUE) | (t["chg"].abs() >= 10)]
    out = []
    for _, r in sub.iterrows():
      try:
        if not ok(r["close"] >= MIN_PRICE) or not ok(r.get("valid_bar", True)) or pd.isna(r["runup"]):
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
                "close": int(r["close"]), "chg": num(r["chg"], 2),
                "runup": num(r["runup"], 2),
                "pos_52w": num(r["pos_52w"] * 100, 1),
                "flags": flags,
            })
      except Exception as e:
        print(f"  ! 경고 판정 건너뜀 {r.get('code', '?')}: {type(e).__name__} {e}", file=sys.stderr)
        continue
    out.sort(key=lambda x: -(x["runup"] or 0))
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
    regime = [market_regime(df, "KOSPI"), market_regime(df, "KOSDAQ")]
    falling = {r["market"]: r.get("falling", False) for r in regime}
    all_a = screen_track_a(t, sectors, falling)
    all_b = screen_track_b(t, sectors, falling)
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
        "regime": regime,
        "track_a": track_a,
        "track_b": track_b,
        "near": near,
        "warnings": warnings,
        "config": {
            "A_MIN_VALUE_eok": int(A_MIN_VALUE / 1e8),
            "A_CHG": [A_CHG_MIN, A_CHG_MAX],
            "A_MAX_MKTCAP_jo": A_MAX_MKTCAP / 1e12,
            "RECENT_LOW_DAYS": RECENT_LOW_DAYS,
            "A_MAX_UP_FROM_LOW_pct": round((A_MAX_RUNUP_RECENT - 1) * 100, 1),
            "B_FROM_HIGH_MAX": B_FROM_HIGH_MAX,
            "B_BOX_DAYS": B_BOX_DAYS,
            "B_VOL_ABS_man": int(B_VOL_ABS / 1e4),
            "B_VOL_VS_PREV": B_VOL_VS_PREV,
            "TARGET1_PCT": TARGET1_PCT,
            "TARGET2_PCT": TARGET2_PCT,
            "MAX_STOP_PCT": MAX_STOP_PCT,
            "MIN_RR": MIN_RR,
            "sector_map": ok(sectors),
        },
    }

    print("[4/4] 저장")
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=1, allow_nan=False), encoding="utf-8")
    print(f"      트랙A {len(track_a)}종목 / 트랙B {len(track_b)}종목 / "
          f"1개조건 미달 {len(near)}종목 / 경고 {len(warnings)}건")
    print(f"      -> {OUT_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
