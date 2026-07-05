# -*- coding: utf-8 -*-
"""
KIS 감시 엔진 v3.2 — GitHub Actions 자동 실행판
- 저장소의 SEPA results.json에서 9점 이상 종목을 감시 목록으로 자동 구성
- 장중 분봉을 순찰하며 [거래량 급증 + 20분 고점 돌파 + SEPA 매수참고가 돌파] 시 텔레그램 알림
- 신호는 규칙 기반 알림이며 예측·매수추천이 아님 (자동 주문 없음)
- v3.2 변경점:
  A. 비정상 종료 시 텔레그램 크래시 알림 + API 연속 실패 감지(토큰 재발급 → 실패 시 경고 종료)
  C. 분봉 신선도 체크(5분 초과 낡은 데이터 = 휴장/거래정지 → 판정 제외)
  D. 마지막 '완성봉' 기준 판정 + SEPA buy_ref 돌파 조건 추가(횡보장 오탐 감소)
  E. results.json 기준일이 오래되면 시작 알림에 경고 표시
  (B. 교대 중복 방지는 monitor.yml의 concurrency로 처리)
"""

import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone

# ===== 환경변수 (GitHub Secrets) =====
APP_KEY    = os.environ.get("KIS_APP_KEY", "")
APP_SECRET = os.environ.get("KIS_APP_SECRET", "")
TG_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT    = os.environ.get("TELEGRAM_CHAT_ID", "")
BASE_URL   = os.environ.get("KIS_BASE_URL", "https://openapivts.koreainvestment.com:29443")

# ===== 감시 설정 =====
WATCH_MIN_SCORE = 9        # SEPA 9점 이상만 감시
WATCH_MAX = 10             # 최대 감시 종목 수
SURGE_RATIO = 2.0          # 거래량 급증 기준 (평균 대비 배수)
LOOKBACK = 20              # 평균/고점 계산 구간 (완성봉 기준, 분)
API_DELAY = 0.6            # KIS 호출 간격
INTERVAL_SEC = 60          # 순찰 주기 (초)
ALERT_COOLDOWN_MIN = 30    # 같은 종목 재알림 최소 간격 (분)
SESSION_MAX_MIN = 190      # 이번 교대의 최대 감시 시간 (분)
MARKET_OPEN  = (9, 0)      # 장 시작
MARKET_CLOSE = (15, 15)    # 감시 종료 시각 (동시호가 왜곡 회피)
FRESH_LIMIT_SEC = 300      # 분봉 신선도 한계 (5분) — 초과 시 휴장/거래정지로 간주
FAIL_LIMIT = 30            # API 연속 실패 허용 횟수 (초과 시 토큰 재발급 시도)
STALE_SCAN_DAYS = 4        # results.json 기준일 경고 한계 (일)

KST = timezone(timedelta(hours=9))


def now_kst():
    return datetime.now(KST)


def tg_send(msg):
    """텔레그램 알림 (미설정 시 로그로만 출력)"""
    print(f"[알림] {msg}")
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": msg},
            timeout=10,
        )
    except Exception as e:
        print(f"[경고] 텔레그램 전송 실패: {e}")


def get_token():
    url = f"{BASE_URL}/oauth2/tokenP"
    body = {"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET}
    data = requests.post(url, headers={"content-type": "application/json"},
                         data=json.dumps(body), timeout=15).json()
    if "access_token" not in data:
        raise RuntimeError(f"토큰 발급 실패: {data}")
    print("[OK] 토큰 발급 성공")
    return data["access_token"]


def load_watchlist():
    """저장소에 커밋된 SEPA results.json에서 감시 목록 구성"""
    with open("results.json", encoding="utf-8") as f:
        d = json.load(f)
    picks = [r for r in d["results"] if r["score"] >= WATCH_MIN_SCORE][:WATCH_MAX]
    print(f"[OK] SEPA 기준일 {d['scan_date']} — {len(picks)}종목 감시")
    for r in picks:
        print(f"  [{r['score']}점] {r['name']} ({r['code']}) 매수참고 {r['buy_ref']:,}원")
    return picks, d.get("scan_date", "")


def get_minute_chart(token, stock_code):
    """당일 분봉 (성공: 시간 오름차순 리스트 / 실패: None)"""
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY, "appsecret": APP_SECRET,
        "tr_id": "FHKST03010200",
    }
    params = {
        "FID_ETC_CLS_CODE": "", "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": stock_code, "FID_INPUT_HOUR_1": "",
        "FID_PW_DATA_INCU_YN": "Y",
    }
    try:
        data = requests.get(url, headers=headers, params=params, timeout=15).json()
    except Exception:
        return None
    if data.get("rt_cd") != "0":
        return None
    rows = data.get("output2", [])
    return sorted(rows, key=lambda r: r.get("stck_cntg_hour", ""))


def is_fresh(rows):
    """마지막 분봉이 현재 시각 기준 5분 이내인지 (휴장일·거래정지·VI 필터)"""
    t = rows[-1].get("stck_cntg_hour", "")
    if len(t) < 4 or not t[:4].isdigit():
        return False
    now = now_kst()
    now_sec = now.hour * 3600 + now.minute * 60 + now.second
    bar_sec = int(t[:2]) * 3600 + int(t[2:4]) * 60
    return abs(now_sec - bar_sec) <= FRESH_LIMIT_SEC


def judge_signal(rows, buy_ref):
    """
    마지막 '완성봉'(rows[-2]) 기준 판정 — 형성 중인 봉의 부분 거래량 오판 방지
    fire 조건: 거래량 급증 AND 직전 고점 돌파 AND SEPA 매수참고가(buy_ref) 이상
    """
    if not rows or len(rows) < LOOKBACK + 2:
        return None
    if not is_fresh(rows):
        return None  # 낡은 데이터 → 휴장/거래정지
    try:
        window = rows[-(LOOKBACK + 2):-2]          # 완성봉 직전 LOOKBACK개
        cur    = rows[-2]                           # 마지막 완성봉
        vols   = [float(r["cntg_vol"]) for r in window]
        highs  = [float(r["stck_hgpr"]) for r in window]
        cur_vol = float(cur["cntg_vol"])
        cur_px  = float(cur["stck_prpr"])
    except Exception:
        return None
    avg_vol = sum(vols) / len(vols)
    prev_hi = max(highs)
    ratio = cur_vol / avg_vol if avg_vol > 0 else 0
    return {
        "price": cur_px, "ratio": ratio, "prev_high": prev_hi,
        "fire": (ratio >= SURGE_RATIO)
                and (cur_px > prev_hi)
                and (cur_px >= float(buy_ref)),
    }


def hm(t):
    return t[0] * 60 + t[1]


def scan_date_age_days(scan_date):
    try:
        d = datetime.strptime(scan_date, "%Y%m%d").replace(tzinfo=KST)
        return (now_kst() - d).days
    except Exception:
        return 999


def main():
    if not APP_KEY or not APP_SECRET:
        raise RuntimeError("KIS_APP_KEY / KIS_APP_SECRET Secrets가 비어있습니다")

    now = now_kst()
    if now.weekday() >= 5:
        print("주말 — 감시 생략")
        return
    if now.hour * 60 + now.minute >= hm(MARKET_CLOSE):
        print("장 종료 후 — 감시 생략")
        return

    # 장 시작 전이면 09:00까지 대기 (예약 실행이 08:50에 뜨는 경우)
    while True:
        now = now_kst()
        if now.hour * 60 + now.minute >= hm(MARKET_OPEN):
            break
        print(f"[{now.strftime('%H:%M:%S')}] 장 시작 대기…")
        time.sleep(30)

    token = get_token()
    watch, scan_date = load_watchlist()

    session_end = min(
        now_kst() + timedelta(minutes=SESSION_MAX_MIN),
        now_kst().replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0),
    )
    names = ", ".join(r["name"] for r in watch)
    stale_mark = ""
    age = scan_date_age_days(scan_date)
    if age > STALE_SCAN_DAYS:
        stale_mark = f"\n⚠️ 주의: SEPA 데이터가 {age}일 전 기준입니다 — 스캐너 작동 확인 필요"
    tg_send(f"👀 KIS 감시 시작 ({now_kst().strftime('%H:%M')}~{session_end.strftime('%H:%M')})\n"
            f"SEPA 기준일 {scan_date} | 감시 {len(watch)}종목: {names}\n"
            f"조건: 거래량 {SURGE_RATIO}배↑ + {LOOKBACK}분 고점 돌파 + 매수참고가 돌파{stale_mark}")

    last_alert = {}
    total_alerts = 0
    cycle = 0
    consec_fail = 0
    token_refreshed = False

    while now_kst() < session_end:
        cycle += 1
        fired = []
        for r in watch:
            rows = get_minute_chart(token, r["code"])
            time.sleep(API_DELAY)

            # --- API 연속 실패 감지 ---
            if rows is None:
                consec_fail += 1
                if consec_fail >= FAIL_LIMIT:
                    if not token_refreshed:
                        print(f"[경고] API {FAIL_LIMIT}회 연속 실패 — 토큰 재발급 시도")
                        try:
                            token = get_token()
                            token_refreshed = True
                            consec_fail = 0
                            tg_send("♻️ API 연속 실패로 토큰을 재발급했습니다 — 감시 계속")
                        except Exception as e:
                            tg_send(f"🚨 토큰 재발급 실패 — 감시 중단: {e}")
                            return
                    else:
                        tg_send(f"🚨 토큰 재발급 후에도 API {FAIL_LIMIT}회 연속 실패 — 감시 중단 "
                                f"(KIS 서버 점검 또는 키 문제 의심)")
                        return
                continue
            consec_fail = 0

            sig = judge_signal(rows, r["buy_ref"])
            if not sig or not sig["fire"]:
                continue
            prev = last_alert.get(r["code"])
            if prev and (now_kst() - prev).total_seconds() < ALERT_COOLDOWN_MIN * 60:
                continue  # 쿨다운 중 — 같은 종목 도배 방지
            last_alert[r["code"]] = now_kst()
            fired.append((r, sig))

        for r, sig in fired:
            total_alerts += 1
            tg_send(
                f"🔥 진입 신호 — {r['name']} ({r['code']})\n"
                f"완성봉 종가 {sig['price']:,.0f}원 | 거래량 {sig['ratio']:.1f}배 | "
                f"{LOOKBACK}분 고점 {sig['prev_high']:,.0f}원 돌파\n"
                f"SEPA {r['score']}점 | 매수참고 {r['buy_ref']:,}원 · 손절참고 {r['stop_ref']:,}원 · 목표 {r['target_ref']:,}원\n"
                f"※ 규칙 기반 알림 — 최종 판단은 직접"
            )

        stamp = now_kst().strftime("%H:%M:%S")
        print(f"[{stamp}] 순찰 {cycle} — 신호 {len(fired)}건 (누적 {total_alerts})")
        time.sleep(INTERVAL_SEC)

    tg_send(f"✅ 감시 종료 ({now_kst().strftime('%H:%M')}) — 순찰 {cycle}회, 알림 {total_alerts}건")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # A. 어떤 이유로든 죽으면 반드시 폰으로 알림
        tg_send(f"🚨 감시 프로그램 비정상 종료: {e}")
        raise
