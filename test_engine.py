#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""daily_setup.py 로직 검증 — 합성 데이터 + 실제 캔들 1건"""
import datetime as dt
import pandas as pd
import daily_setup as ds

FAIL = []

def check(name, cond, detail=""):
    print(("  OK   " if cond else "  FAIL ") + name + ("  " + detail if detail else ""))
    if not cond:
        FAIL.append(name)

# ------------------------------------------------------------------
print("\n[1] 호가단위 반올림")
cases = [(1234, 1234), (4321, 4320), (17020, 17020), (33333, 33350),
         (123456, 123500), (321000, 321000), (1653000, 1653000)]
for raw, want in cases:
    got = ds.round_tick(raw)
    check(f"round_tick({raw}) -> {got}", got == want, f"기대 {want}")
check("호가 올림 18620*1.001 -> 18640(10원 단위)", ds.round_tick(18620 * 1.001, "up") == 18640,
      str(ds.round_tick(18620 * 1.001, "up")))
check("호가 내림 14650*0.99 -> 14500", ds.round_tick(14650 * 0.99, "down") == 14500,
      str(ds.round_tick(14650 * 0.99, "down")))

# ------------------------------------------------------------------
print("\n[2] 매매계획 — 실제 캔들(금호건설 2026-08-28 O15250 H18620 L14650 C17020)")
row = pd.Series({"open": 15250, "high": 18620, "low": 14650, "close": 17020})
p = ds.make_plan(row)
print("      ", p)
check("돌파매수 = 기준봉 고가 위", p["buy_break"] == 18640)
check("손절폭 10% 이내", 0 < -p["stop_pct"] <= 10.0, f'{p["stop_pct"]}%')
check("손절가 하한(=매수가-10%) 적용", p["stop"] == ds.round_tick(18640 * 0.9, "up"),
      f'stop={p["stop"]}')
check("1차 익절 = 매수가 +10%", abs(p["target1"] / p["buy_break"] - 1.10) < 0.005)
check("2차 익절 = 매수가 +20%", abs(p["target2"] / p["buy_break"] - 1.20) < 0.005)
check("손익비 1.0 이상", p["rr"] >= 1.0, f'RR={p["rr"]}')
check("고저폭 27% 캔들은 변동성 과대로 제외", p["ok"] is False and any("변동성" in r for r in p["reject"]),
      str(p["reject"]))
check("눌림매수 = 실체 절반", p["buy_pull"] == ds.round_tick(15250 + (17020 - 15250) * 0.5))

# 손절폭이 좁은 정상 케이스
row2 = pd.Series({"open": 10000, "high": 10600, "low": 9900, "close": 10550})
p2 = ds.make_plan(row2)
check("저가 이탈 손절이 -10% 안쪽이면 저가 기준 사용",
      p2["stop"] == ds.round_tick(9900 * 0.99, "down"), str(p2))

# ------------------------------------------------------------------
print("\n[3] 합성 데이터 스크리닝")

def bars(code, name, market, seq, mktcap=500_000_000_000):
    """seq: [(open,high,low,close,volume)] 오래된 순"""
    base = dt.date(2026, 4, 1)
    rows, d = [], base
    prev = None
    for i, (o, h, l, c, v) in enumerate(seq):
        while d.weekday() >= 5:
            d += dt.timedelta(days=1)
        chg = 0.0 if prev is None else (c / prev - 1) * 100
        rows.append({"date": d.strftime("%Y%m%d"), "code": code, "name": name,
                     "market": market, "open": o, "high": h, "low": l, "close": c,
                     "chg": round(chg, 2), "volume": v, "value": c * v, "mktcap": mktcap})
        prev = c
        d += dt.timedelta(days=1)
    return rows

N = 200
# (A) 주도주 시작점: 완만 횡보 후 마지막날 +8% 장대양봉 + 거래대금 2,700억, 시총 0.5조
a = [(10000, 10100, 9900, 10000, 300_000) for _ in range(N - 1)]
a += [(10050, 10850, 10000, 10800, 25_000_000)]
# (A-2) 대형주: 조건은 같지만 시총 50조 → 김종봉 지적대로 제외돼야 함
a2 = list(a)
# (A-3) 등락률 과다: +14% → 원문 5~10% 범위 밖
a3 = [(10000, 10100, 9900, 10000, 300_000) for _ in range(N - 1)]
a3 += [(10050, 11450, 10000, 11400, 25_000_000)]
# (B) 바닥권 턴어라운드: 고점 20,000 → 폭락 → 150일 횡보 → 300만 주 장대양봉
b = [(20000, 20200, 19800, 20000, 200_000) for _ in range(40)]
b += [(20000 - i * 1600, 20100 - i * 1600, 19700 - i * 1600, 19800 - i * 1600, 400_000)
      for i in range(9)]
b += [(5000, 5100, 4900, 5000, 100_000) for _ in range(N - 1 - len(b))]
b += [(5050, 5600, 5020, 5550, 3_200_000)]
# (C) 고점 과열: 바닥 대비 5배 급등 + 긴 윗꼬리
c = [(2000 + i * 36, 2100 + i * 36, 1950 + i * 36, 2050 + i * 36, 500_000) for i in range(N - 1)]
c += [(9200, 12000, 9100, 9900, 30_000_000)]

df = pd.DataFrame(
    bars("111111", "가상주도주", "KOSPI", a)
    + bars("444444", "가상대형주", "KOSPI", a2, mktcap=50_000_000_000_000)
    + bars("555555", "가상급등주", "KOSPI", a3)
    + bars("222222", "가상턴어라운드", "KOSDAQ", b)
    + bars("333333", "가상과열주", "KOSPI", c)
).sort_values(["code", "date"]).reset_index(drop=True)

today = sorted(df["date"].unique())[-1]
t = ds.build_features(df, today)
ta = [x for x in ds.screen_track_a(t, {"111111": "건설", "333333": "건설"}) if x["status"] == "pass"]
tb = [x for x in ds.screen_track_b(t, {}) if x["status"] == "pass"]
near = [x for x in ds.screen_track_a(t, {}) + ds.screen_track_b(t, {}) if x["status"] == "near"]
wn = ds.screen_warnings(t, set())
check("1개 조건 미달 종목은 near 로 분리", all(len(x["missing"]) == 1 for x in near),
      str([(x["name"], x["missing"]) for x in near]))

check("트랙A가 주도주 1종목만 선별", [x["code"] for x in ta] == ["111111"],
      str([x["name"] for x in ta]))
check("시총 10조 초과 대형주 제외 (김종봉 지적)",
      "444444" not in [x["code"] for x in ta], str([x["name"] for x in ta]))
check("등락률 +14% 는 원문 5~10% 밖이라 제외",
      "555555" not in [x["code"] for x in ta], str([x["name"] for x in ta]))
check("트랙B가 턴어라운드 1종목만 선별", [x["code"] for x in tb] == ["222222"],
      str([x["name"] for x in tb]))
if tb:
    check("고점 대비 65% 이상 하락한 바닥권", tb[0]["from_high_pct"] >= 65,
          f'고점대비 {tb[0]["from_high_pct"]}% 하락')
    check("절대 거래량 300만 주 이상", tb[0]["vol_man"] >= 300,
          f'{tb[0]["vol_man"]}만 주')
check("과열주는 두 트랙 모두 제외",
      "333333" not in [x["code"] for x in ta + tb])
check("과열주가 고점 위험 경고에 포착",
      "333333" in [x["code"] for x in wn], str([(x["name"], x["flags"]) for x in wn]))
if ta:
    print("      트랙A:", {k: ta[0][k] for k in ("name", "chg", "value_eok", "value_surge", "runup")})
    print("      계획 :", ta[0]["plan"])
if tb:
    print("      트랙B:", {k: tb[0][k] for k in ("name", "chg", "pos_52w", "vol_vs_prev", "vol_vs_avg60")})
    print("      계획 :", tb[0]["plan"])

# ------------------------------------------------------------------
print("\n[4] 시장 국면")
r = ds.market_regime(df, "KOSPI")
print("      ", r)
check("시장 국면 상태값 산출", r.get("state") not in (None, "데이터부족"), str(r.get("state")))

# ------------------------------------------------------------------
print("\n[5] 제외 필터")
check("우선주 제외", not ds.is_tradable(pd.Series(
    {"name": "삼성전자우", "close": 180000, "mktcap": 1e13, "days": 200})))
check("스팩 제외", not ds.is_tradable(pd.Series(
    {"name": "엔에이치스팩30호", "close": 2000, "mktcap": 1e11, "days": 200})))
check("동전주 제외", not ds.is_tradable(pd.Series(
    {"name": "저가주", "close": 800, "mktcap": 1e11, "days": 200})))
check("정상 종목 통과", ds.is_tradable(pd.Series(
    {"name": "현대차", "close": 399500, "mktcap": 1e13, "days": 200})))

print("\n" + ("=" * 46))
print("실패 " + str(len(FAIL)) + "건" + ((": " + ", ".join(FAIL)) if FAIL else " — 전체 통과"))

# ------------------------------------------------------------------
print("\n[6] 엔드투엔드 — GitHub Actions 가 실제로 실행하는 경로 그대로")
# (스크리닝 함수만 테스트하면 main() 안의 오타·잔여 참조를 못 잡는다.
#  실제로 daily_setup.py 를 프로세스로 띄워 JSON 이 만들어지는지 확인한다.)
import subprocess, tempfile, shutil, os, json as _json

_tmp = tempfile.mkdtemp()
_cache = os.path.join(_tmp, "data", "ohlcv")
os.makedirs(_cache, exist_ok=True)
_df = df.copy()
for _d, _g in _df.groupby(_df["date"].str[:6]):
    _g.to_csv(os.path.join(_cache, f"{_d}.csv.gz"), index=False, compression="gzip")
for _f in ("daily_setup.py", "dart_info.py"):
    if os.path.exists(_f):
        shutil.copy(_f, _tmp)

_env = dict(os.environ); _env.pop("DART_API_KEY", None)   # 키 없이도 돌아야 한다
_r = subprocess.run(["python3", "daily_setup.py", "--no-fetch"],
                    cwd=_tmp, capture_output=True, text=True, env=_env)
check("daily_setup.py --no-fetch 정상 종료", _r.returncode == 0,
      (_r.stderr or _r.stdout).strip().splitlines()[-1] if _r.returncode else "")
_out = os.path.join(_tmp, "data", "daily.json")
check("daily.json 생성됨", os.path.exists(_out))
if os.path.exists(_out):
    _p = _json.load(open(_out, encoding="utf-8"))
    for _k in ("generated_at", "base_date", "universe", "regime",
               "track_a", "track_b", "track_c", "near", "warnings", "config"):
        check(f"payload 키 '{_k}'", _k in _p)
    check("config 에 트랙C 수치 포함",
          "C_MAX_UP_FROM_LOW_pct" in _p.get("config", {}))
    check("JSON 에 NaN 없음", "NaN" not in open(_out, encoding="utf-8").read())
    print("      실행 로그:", [l for l in _r.stdout.splitlines() if "트랙" in l])
shutil.rmtree(_tmp, ignore_errors=True)

print("\n" + ("=" * 46))
print("최종: 실패 " + str(len(FAIL)) + "건" + ((": " + ", ".join(FAIL)) if FAIL else " — 전체 통과"))
