# -*- coding: utf-8 -*-
"""
KIS 감시 엔진 v3.0 — GitHub Actions 자동 실행판
- 저장소의 SEPA results.json에서 9점 이상 종목을 감시 목록으로 자동 구성
- 장중 분봉을 순찰하며 [거래량 급증 + 직전 고점 돌파] 동시 충족 시 텔레그램 알림
- 신호는 규칙 기반 알림이며 예측·매수추천이 아님 (자동 주문 없음)
- 오전조/오후조 2교대 실행 (GitHub 작업당 6시간 제한 대응)
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
WATCH_MIN_SCORE = 9       # SEPA 9점 이상만 감시
WATCH_MAX = 10            # 최대 감시 종목 수
SURGE_RATIO = 2.0         # 거래량 급증 기준 (평균 대비 배수)
LOOKBACK = 20             # 평균/고점 계산 구간 (분)
API_DELAY = 0.6           # KIS 호출 간격
INTERVAL_SEC = 60         # 순찰 주기 (초)
ALERT_COOLDOWN_MIN = 30   # 같은 종목 재알림 최소 간격 (분)
SESSION_MAX_MIN = 190     # 이번 교대의 최대 감시 시간 (분)
MARKET_OPEN  = (9, 0)     # 장 시작
MARKET_CLOSE = (15, 15)   # 감시 종료 시각 (동시호가 왜곡 회피)

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
    return picks


def get_minute_chart(token, stock_code):
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


def judge_signal(rows):
    if not rows or len(rows) < LOOKBACK + 1:
        return None
    try:
        vols   = [float(r["cntg_vol"]) for r in rows]
        highs  = [float(r["stck_hgpr"]) for r in rows]
        closes = [float(r["stck_prpr"]) for r in rows]
    except Exception:
        return None
    cur_vol = vols[-1]
    avg_vol = sum(vols[-(LOOKBACK + 1):-1]) / LOOKBACK
    cur_px  = closes[-1]
    prev_hi = max(highs[-(LOOKBACK + 1):-1])
    ratio = cur_vol / avg_vol if avg_vol > 0 else 0
    return {
        "price": cur_px, "ratio": ratio, "prev_high": prev_hi,
        "fire": (ratio >= SURGE_RATIO) and (cur_px > prev_hi),
    }


def hm(t):
    return t[0] * 60 + t[1]


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
    watch = load_watchlist()

    session_end = min(
        now_kst() + timedelta(minutes=SESSION_MAX_MIN),
        now_kst().replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0),
    )
    names = ", ".join(r["name"] for r in watch)
    tg_send(f"👀 KIS 감시 시작 ({now_kst().strftime('%H:%M')}~{session_end.strftime('%H:%M')})\n"
            f"감시 {len(watch)}종목: {names}\n"
            f"조건: 거래량 {SURGE_RATIO}배↑ + {LOOKBACK}분 고점 돌파")

    last_alert = {}
    total_alerts = 0
    cycle = 0

    while now_kst() < session_end:
        cycle += 1
        fired = []
        for r in watch:
            rows = get_minute_chart(token, r["code"])
            sig = judge_signal(rows)
            time.sleep(API_DELAY)
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
                f"현재가 {sig['price']:,.0f}원 | 거래량 {sig['ratio']:.1f}배 | "
                f"{LOOKBACK}분 고점 {sig['prev_high']:,.0f}원 돌파\n"
                f"SEPA {r['score']}점 | 매수참고 {r['buy_ref']:,}원 · 손절참고 {r['stop_ref']:,}원 · 목표 {r['target_ref']:,}원\n"
                f"※ 규칙 기반 알림 — 최종 판단은 직접"
            )

        stamp = now_kst().strftime("%H:%M:%S")
        print(f"[{stamp}] 순찰 {cycle} — 신호 {len(fired)}건 (누적 {total_alerts})")
        time.sleep(INTERVAL_SEC)

    tg_send(f"✅ 감시 종료 ({now_kst().strftime('%H:%M')}) — 순찰 {cycle}회, 알림 {total_alerts}건")


if __name__ == "__main__":
    main()
