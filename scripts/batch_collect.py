"""DART 공시목록 API(list.json)로 배치 검증 대상 접수번호를 수집한다.

사용: ``uv run python scripts/batch_collect.py [--months 18] [--seed 42] [--max-pages 20]``
출력: ``batch/targets.csv`` (rcept_no, corp_name, corp_cls, report_nm, rcept_dt, 유형).

``DART_API_KEY`` 는 ``.env`` 에서만 읽으며(python-dotenv), 로그·CSV 어디에도 찍지 않는다.
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

LIST_URL = "https://opendart.fss.or.kr/api/list.json"
PAGE_SLEEP_SEC = 0.3
PAGE_COUNT = 100

# (유형 라벨, pblntf_ty, pblntf_detail_ty, report_nm 필수 포함, report_nm 제외, corp_cls 또는 None)
SpecT = tuple[str, str, str, str, str, str | None]


def _load_api_key() -> str:
    """.env 의 DART_API_KEY 를 읽는다. 없으면 종료."""
    import os

    load_dotenv()
    key = os.environ.get("DART_API_KEY", "").strip()
    if not key:
        sys.exit(".env 에 DART_API_KEY 가 없습니다. .env.example 을 참고해 .env 를 만드세요.")
    return key


def _fetch_pages(
    key: str, bgn_de: str, end_de: str, pblntf_ty: str, detail_ty: str,
    corp_cls: str | None, max_pages: int,
) -> list[dict]:
    """list.json 을 페이지 단위로 수집한다. status != "000" 이면 메시지와 함께 종료."""
    rows: list[dict] = []
    page = 1
    while page <= max_pages:
        params = {
            "crtfc_key": key, "bgn_de": bgn_de, "end_de": end_de,
            "pblntf_ty": pblntf_ty, "pblntf_detail_ty": detail_ty,
            "page_no": page, "page_count": PAGE_COUNT, "sort": "date", "sort_mth": "desc",
        }
        if corp_cls:
            params["corp_cls"] = corp_cls
        data = requests.get(LIST_URL, params=params, timeout=30).json()
        status = data.get("status")
        if status == "013":  # 조회 결과 없음
            break
        if status != "000":
            sys.exit(f"list.json 오류 status={status}: {data.get('message', '')}")
        rows.extend(data.get("list", []))
        if page >= int(data.get("total_page", 1)):
            break
        page += 1
        time.sleep(PAGE_SLEEP_SEC)
    return rows


def _sample(rows: list[dict], include: str, exclude: str, n: int, rng: random.Random, seen_corp: set[str]) -> list[dict]:
    """report_nm 필터 후 회사 중복을 제거하고 무작위 표본 n개를 뽑는다."""
    pool = [
        r for r in rows
        if include in r.get("report_nm", "") and (not exclude or exclude not in r.get("report_nm", ""))
    ]
    rng.shuffle(pool)
    picked: list[dict] = []
    for r in pool:
        corp = r.get("corp_code") or r.get("corp_name")
        if corp in seen_corp:
            continue
        seen_corp.add(corp)
        picked.append(r)
        if len(picked) >= n:
            break
    return picked


def main() -> None:
    ap = argparse.ArgumentParser(description="배치 검증 대상 수집")
    ap.add_argument("--months", type=int, default=18, help="수집 기간 (개월, 기본 18)")
    ap.add_argument("--seed", type=int, default=42, help="표본 추출 seed (재현용, 기본 42)")
    ap.add_argument("--max-pages", type=int, default=20, help="쿼리당 최대 페이지 (기본 20 = 2,000건)")
    ap.add_argument("--annual-y", type=int, default=20)
    ap.add_argument("--annual-k", type=int, default=20)
    ap.add_argument("--annual-e", type=int, default=10)
    ap.add_argument("--half", type=int, default=10)
    ap.add_argument("--quarter", type=int, default=10)
    ap.add_argument("--audit", type=int, default=40)
    ap.add_argument("--audit-consol", type=int, default=40)
    ap.add_argument("--out", default="batch/targets.csv")
    args = ap.parse_args()

    key = _load_api_key()
    end = date.today()
    bgn = end - timedelta(days=args.months * 30)
    bgn_de, end_de = bgn.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    rng = random.Random(args.seed)
    seen_corp: set[str] = set()

    specs: list[tuple[str, str, str, str, str, str | None, int]] = [
        ("사업보고서", "A", "A001", "사업보고서", "", "Y", args.annual_y),
        ("사업보고서", "A", "A001", "사업보고서", "", "K", args.annual_k),
        ("사업보고서", "A", "A001", "사업보고서", "", "E", args.annual_e),
        ("반기보고서", "A", "A002", "반기보고서", "", None, args.half),
        ("분기보고서", "A", "A003", "분기보고서", "", None, args.quarter),
        ("감사보고서", "F", "F001", "감사보고서", "연결", None, args.audit),
        ("연결감사보고서", "F", "F002", "연결감사보고서", "", None, args.audit_consol),
    ]

    out_rows: list[dict] = []
    for label, ty, detail, include, exclude, cls, n in specs:
        if n <= 0:
            continue
        rows = _fetch_pages(key, bgn_de, end_de, ty, detail, cls, args.max_pages)
        picked = _sample(rows, include, exclude, n, rng, seen_corp)
        print(f"{label}({cls or '전체'}): 후보 {len(rows)}건 → 표본 {len(picked)}건")
        for r in picked:
            out_rows.append({
                "rcept_no": r["rcept_no"], "corp_name": r.get("corp_name", ""),
                "corp_cls": r.get("corp_cls", ""), "report_nm": r.get("report_nm", "").strip(),
                "rcept_dt": r.get("rcept_dt", ""), "유형": label,
            })
        time.sleep(PAGE_SLEEP_SEC)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["rcept_no", "corp_name", "corp_cls", "report_nm", "rcept_dt", "유형"])
        w.writeheader()
        w.writerows(out_rows)
    print(f"총 {len(out_rows)}건 → {out}")


if __name__ == "__main__":
    main()
