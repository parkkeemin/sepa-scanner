#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dart_info.py — 선별 종목 기업 분석 (DART OpenAPI)

daily_setup.py 가 트랙 A/B 를 통과한 종목에 대해서만 호출합니다.
보통 하루 0~5종목이라 몇 초면 끝납니다.

  "그리고 이제 기업도 체크해 보고 너무 이상한 개똥 같은 회사면 또 안 되니까"
  "이 기업이 뭐 하는 기업이고 이 기업이 어떤 성장성을 가지고 있고…
   두산에너빌리티 누가 분석해 놨지 찾아보면은 누군가가 막 이야기해 주고 있어요."

환경변수 DART_API_KEY 가 없으면 조용히 건너뜁니다(스캔은 정상 동작).
발급: https://opendart.fss.or.kr  (무료, 일 20,000건)
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import zipfile
import datetime as dt
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
CORP_MAP = BASE_DIR / "data" / "corp_map.json"
DART_KEY = os.environ.get("DART_API_KEY", "")
API = "https://opendart.fss.or.kr/api/"

# 공시 제목에서 잡아낼 위험 키워드 (앞쪽이 더 심각)
RISK_KEYWORDS = [
    ("상장폐지",     "심각", "상장폐지 관련 공시"),
    ("관리종목",     "심각", "관리종목 지정 관련"),
    ("감사의견",     "심각", "감사의견 관련 — 거절·한정 여부 확인"),
    ("횡령",         "심각", "횡령·배임 혐의"),
    ("배임",         "심각", "횡령·배임 혐의"),
    ("회생절차",     "심각", "회생절차 관련"),
    ("불성실공시",   "심각", "불성실공시법인 지정"),
    ("감자",         "주의", "감자 — 자본 감소"),
    ("유상증자",     "주의", "유상증자 — 주식 수 증가로 지분 희석"),
    ("전환사채",     "주의", "전환사채(CB) — 주식 전환 시 지분 희석"),
    ("신주인수권부", "주의", "신주인수권부사채(BW) — 지분 희석 가능"),
    ("교환사채",     "주의", "교환사채(EB) 발행"),
    ("최대주주",     "참고", "최대주주 변경 관련"),
    ("소송",         "참고", "소송 관련"),
]


def _get(path: str, **params) -> dict | None:
    params["crtfc_key"] = DART_KEY
    try:
        r = requests.get(API + path, params=params, timeout=20)
        if r.status_code != 200:
            return None
        d = r.json()
        # status 000=정상, 013=조회 데이터 없음
        if d.get("status") not in ("000", "013"):
            print(f"  ! DART {path} status={d.get('status')} {d.get('message','')}",
                  file=sys.stderr)
            return None
        return d
    except (requests.RequestException, ValueError):
        return None


# ------------------------------------------------------------------
# 종목코드 → DART 고유번호(corp_code) 매핑. 하루 1회만 내려받아 캐시.
# ------------------------------------------------------------------
def load_corp_map(max_age_days: int = 7) -> dict:
    if CORP_MAP.exists():
        age = dt.datetime.now() - dt.datetime.fromtimestamp(CORP_MAP.stat().st_mtime)
        if age.days < max_age_days:
            try:
                return json.loads(CORP_MAP.read_text(encoding="utf-8"))
            except Exception:
                pass
    if not DART_KEY:
        return {}
    try:
        r = requests.get(API + "corpCode.xml", params={"crtfc_key": DART_KEY}, timeout=60)
        if r.status_code != 200 or not r.content[:2] == b"PK":
            print("  ! DART corpCode 내려받기 실패", file=sys.stderr)
            return {}
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            xml = z.read(z.namelist()[0])
        root = ET.fromstring(xml)
        m = {}
        for it in root.iter("list"):
            sc = (it.findtext("stock_code") or "").strip()
            cc = (it.findtext("corp_code") or "").strip()
            if sc and sc != " " and cc:
                m[sc.zfill(6)] = cc
        CORP_MAP.parent.mkdir(parents=True, exist_ok=True)
        CORP_MAP.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        print(f"  DART 기업코드 {len(m):,}건 갱신")
        return m
    except Exception as e:
        print(f"  ! DART corpCode 오류: {type(e).__name__} {e}", file=sys.stderr)
        return {}


def _num(v) -> float | None:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _financials(corp_code: str) -> dict | None:
    """직전 사업연도 주요계정. 아직 미공시면 그 전년도로 한 번 더 시도."""
    for back in (1, 2):
        year = str(dt.date.today().year - back)
        d = _get("fnlttSinglAcnt.json", corp_code=corp_code,
                 bsns_year=year, reprt_code="11011")
        rows = (d or {}).get("list") or []
        if not rows:
            continue
        # 연결 우선, 없으면 개별
        pick = {}
        for pref in ("CFS", "OFS"):
            sub = [x for x in rows if x.get("fs_div") == pref]
            if sub:
                pick = {x.get("account_nm"): _num(x.get("thstrm_amount")) for x in sub}
                break
        if not pick:
            pick = {x.get("account_nm"): _num(x.get("thstrm_amount")) for x in rows}
        rev = pick.get("매출액")
        op = pick.get("영업이익")
        net = pick.get("당기순이익")
        eq = pick.get("자본총계")
        li = pick.get("부채총계")
        return {
            "year": year,
            "revenue": rev, "operating_income": op, "net_income": net,
            "total_equity": eq, "total_liabilities": li,
            "op_margin": round(op / rev * 100, 1) if (rev and op is not None and rev > 0) else None,
            "debt_ratio": round(li / eq * 100, 1) if (eq and li is not None and eq > 0) else None,
        }
    return None


def _filings(corp_code: str, days: int = 90) -> list[dict]:
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    d = _get("list.json", corp_code=corp_code,
             bgn_de=start.strftime("%Y%m%d"), end_de=end.strftime("%Y%m%d"),
             page_count=100, last_reprt_at="Y")
    out = []
    for it in (d or {}).get("list") or []:
        title = (it.get("report_nm") or "").strip()
        level, why = None, None
        for kw, lv, msg in RISK_KEYWORDS:
            if kw in title:
                level, why = lv, msg
                break
        out.append({
            "date": it.get("rcept_dt", ""),
            "title": title,
            "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={it.get('rcept_no','')}",
            "risk": level, "why": why,
        })
    return out


def analyze(stock_code: str, corp_map: dict | None = None) -> dict:
    """한 종목의 기업 정보 + 위험 플래그. 실패해도 예외를 밖으로 던지지 않는다."""
    if not DART_KEY:
        return {"ok": False, "reason": "DART_API_KEY 미설정 — 기업 확인은 수동"}
    try:
        cmap = corp_map if corp_map is not None else load_corp_map()
        cc = cmap.get(str(stock_code).zfill(6))
        if not cc:
            return {"ok": False, "reason": "DART 기업코드 매칭 실패"}

        d = _get("company.json", corp_code=cc) or {}
        fin = _financials(cc)
        fils = _filings(cc)
        time.sleep(0.2)

        flags = []
        if fin:
            op, net, eq, dr = (fin["operating_income"], fin["net_income"],
                               fin["total_equity"], fin["debt_ratio"])
            if op is not None and op < 0:
                flags.append({"level": "심각", "text": f'{fin["year"]}년 영업적자'})
            elif net is not None and net < 0:
                flags.append({"level": "주의", "text": f'{fin["year"]}년 당기순손실'})
            if eq is not None and eq <= 0:
                flags.append({"level": "심각", "text": "자본잠식"})
            if dr is not None and dr > 200:
                flags.append({"level": "주의", "text": f"부채비율 {dr:.0f}%"})
        else:
            flags.append({"level": "참고", "text": "재무 데이터 조회 불가"})

        seen = set()
        for f in fils:
            if f["risk"] and f["why"] not in seen:
                seen.add(f["why"])
                flags.append({"level": f["risk"], "text": f'{f["why"]} ({f["date"][4:6]}/{f["date"][6:]})'})

        if not flags:
            flags.append({"level": "양호", "text": "흑자 · 재무 양호 · 최근 90일 위험 공시 없음"})

        return {
            "ok": True,
            "name": d.get("corp_name"),
            "ceo": d.get("ceo_nm"),
            "industry_code": d.get("induty_code"),
            "established": d.get("est_dt"),
            "homepage": d.get("hm_url"),
            "market": d.get("corp_cls"),
            "fin": fin,
            "flags": flags,
            "filings": fils[:8],
            "dart_url": f"https://dart.fss.or.kr/dsab001/main.do?option=corp&textCrpNm={d.get('corp_name','')}",
        }
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


def enrich(stocks: list[dict]) -> int:
    """선별 종목 리스트에 company 정보를 붙인다. 붙인 종목 수 반환."""
    if not stocks:
        return 0
    if not DART_KEY:
        for s in stocks:
            s["company"] = {"ok": False, "reason": "DART_API_KEY 미설정 — 기업 확인은 수동"}
        return 0
    cmap = load_corp_map()
    n = 0
    for s in stocks:
        s["company"] = analyze(s.get("code", ""), cmap)
        if s["company"].get("ok"):
            n += 1
    return n


if __name__ == "__main__":
    code = sys.argv[1] if len(sys.argv) > 1 else "257720"
    print(json.dumps(analyze(code), ensure_ascii=False, indent=1))
