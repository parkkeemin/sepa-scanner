#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_sector_map.py — 섹터(업종) 매핑표 생성 (선택 사항, 분기 1회면 충분)

트랙 A의 '같은 섹터 동반 급등 확인' 조건에 쓰입니다.
매핑표가 없으면 스캐너는 정상 동작하되 섹터 동반 여부를 '수동 확인'으로 표시합니다.

    pip install finance-datareader
    python build_sector_map.py
    -> data/sector_map.csv  (code,name,sector)
"""
from pathlib import Path
import sys

OUT = Path(__file__).resolve().parent / "data" / "sector_map.csv"

def main() -> int:
    try:
        import FinanceDataReader as fdr
    except ImportError:
        print("finance-datareader 가 없습니다:  pip install finance-datareader", file=sys.stderr)
        return 1

    df = fdr.StockListing("KRX-DESC")          # Code, Name, Sector, Industry
    cols = {c.lower(): c for c in df.columns}
    code = cols.get("code"); name = cols.get("name"); sector = cols.get("sector")
    if not (code and name and sector):
        print(f"예상과 다른 컬럼 구성: {list(df.columns)}", file=sys.stderr)
        return 1

    out = df[[code, name, sector]].dropna()
    out.columns = ["code", "name", "sector"]
    out["code"] = out["code"].astype(str).str.zfill(6)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT, index=False, encoding="utf-8")
    print(f"{len(out):,}종목 저장 -> {OUT}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
