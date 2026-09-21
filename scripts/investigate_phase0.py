"""Phase 0 조사 스크립트: 배치 캐시 오프라인 실측 통계 + fixture 실물 덤프.

기존 모듈은 import 만 하고 수정하지 않는다. 네트워크를 쓰지 않는다
(batch/cache 만 읽는다. 캐시에 없는 노드는 pipeline 이 경고로 처리).

사용: ``uv run python scripts/investigate_phase0.py [--limit N] [--skip-cache] [--skip-dump]``
출력이 길므로 보통 파일로 리다이렉트한다.
"""

from __future__ import annotations

import argparse
import csv
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dart import pipeline  # noqa: E402
from dart.excel import has_note_column  # noqa: E402
from dart.models import Note, Statement, Table  # noqa: E402
from dart.notes import split_notes  # noqa: E402
from dart.statements import extract_statements  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "batch" / "cache"
FIXTURES = ROOT / "tests" / "fixtures"

# ---------- 캐시 전용 fetch (batch_run 과 같은 규칙, 네트워크 없음) ----------

_FETCH_LOG: list[tuple[str, str]] = []  # (url, html) — 문서 하나 처리 동안 누적


def _cache_path(url: str) -> Path | None:
    params = dict(re.findall(r"[?&]([^=&]+)=([^&]*)", url))
    rcp = params.get("rcpNo")
    if not rcp:
        return None
    name = "main.html" if "main.do" in url else f"ele_{params.get('eleId', 'x')}.html"
    return CACHE_DIR / rcp / name


def _offline_fetch(session, url: str) -> str:
    path = _cache_path(url)
    if path is None or not path.exists():
        raise FileNotFoundError(f"캐시 없음(offline): {path}")
    html = path.read_text(encoding="utf-8")
    _FETCH_LOG.append((url, html))
    return html


# ---------- 통계 헬퍼 ----------


def _unit_bucket(unit: str | None) -> str:
    if unit is None:
        return "추출실패(None)"
    u = unit.replace(" ", "")
    if "백만원" in u:
        return "백만원"
    if "천원" in u:
        return "천원"
    if "원" in u:
        return "원"
    return f"기타"


_TOTAL_KEYS = ("소계", "합계", "총계", "순액", "장부금액")
_BS_PREFIX_RE = re.compile(r"^\s*(?:[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]|[IVX]{1,4}|\d{1,2})\s*[.．]|^\s*\(\d+\)")
_NEG_PAREN_RE = re.compile(r"\(\s*\d{1,3}(?:,\d{3})+(?:\.\d+)?\s*\)")
_NEG_TRI_RE = re.compile(r"△\s*\d")
_NEG_TRIF_RE = re.compile(r"▲\s*\d")
_NEG_MINUS_RE = re.compile(r">\s*-\s*\d{1,3}(?:,\d{3})*(?:\.\d+)?\s*<")


def _row_name(row: list) -> str:
    return str(row[0]) if row and isinstance(row[0], str) else ""


def _total_hits(rows: list[list]) -> Counter:
    c: Counter = Counter()
    for r in rows:
        name = re.sub(r"[\s　 ]+", "", _row_name(r))
        if not name:
            continue
        if name == "계":
            c["계(단독)"] += 1
        for k in _TOTAL_KEYS:
            if k in name:
                c[k] += 1
    return c


def _note_tables(notes: list[Note]) -> list[Table]:
    return [b.table for n in notes for b in n.blocks if b.kind == "table" and b.table is not None]


def collect_doc_stats(report, fetched: list[tuple[str, str]], doc_type: str) -> dict:
    """convert_report 결과 하나에서 통계 항목을 뽑는다."""
    d: dict = {"유형": doc_type}
    scopes = {s.scope for s in report.statements} | {n.scope for n in report.notes}
    d["스코프"] = "+".join(sorted(scopes)) or "(없음)"
    # K-IFRS / 일반기업회계기준: 수신한 본문(주석 포함) 텍스트에 문구가 있는지
    all_html = "".join(h for u, h in fetched if "viewer.do" in u)
    if "일반기업회계기준" in all_html:
        d["회계기준"] = "일반기업회계기준"
    elif "한국채택국제회계기준" in all_html or "K-IFRS" in all_html:
        d["회계기준"] = "K-IFRS"
    else:
        d["회계기준"] = "식별불가"

    d["stmt_units"] = Counter(_unit_bucket(s.unit) for s in report.statements)
    nts = _note_tables(report.notes)
    d["note_units"] = Counter(_unit_bucket(t.unit) for t in nts)

    # 재무상태표·손익계산서 주석참조 방식
    ref_kind = set()
    for s in report.statements:
        if s.kind not in ("재무상태표", "손익계산서", "포괄손익계산서"):
            continue
        if any(s.table.note_refs):
            ref_kind.add("계정과목에서 떼어냄")
        if has_note_column(s.table):
            ref_kind.add("원문 주석 열")
    d["ref_kind"] = ref_kind

    rows_total = rows_deep = tables_total = tables_flat = 0
    for s in report.statements:
        depths = s.table.depths
        rows_total += len(depths)
        rows_deep += sum(1 for x in depths if x >= 1)
        tables_total += 1
        tables_flat += 1 if depths and all(x == 0 for x in depths) else (1 if not depths else 0)
    d["rows_total"], d["rows_deep"] = rows_total, rows_deep
    d["tables_total"], d["tables_flat"] = tables_total, tables_flat

    d["stmt_total_hits"] = sum((_total_hits(s.table.rows) for s in report.statements), Counter())
    d["note_total_hits"] = sum((_total_hits(t.rows) for t in nts), Counter())

    bs_prefix = 0
    for s in report.statements:
        if s.kind != "재무상태표":
            continue
        bs_prefix += sum(1 for r in s.table.rows if _BS_PREFIX_RE.match(_row_name(r)))
    d["bs_prefix_rows"] = bs_prefix

    fs_html = "".join(h for u, h in fetched if "viewer.do" in u)
    d["neg"] = {
        "괄호": bool(_NEG_PAREN_RE.search(fs_html)),
        "△": bool(_NEG_TRI_RE.search(fs_html)),
        "▲": bool(_NEG_TRIF_RE.search(fs_html)),
        "선행-": bool(_NEG_MINUS_RE.search(fs_html)),
    }

    d["note_count"] = len(report.notes)
    d["note_table_count"] = len(nts)

    roll_first = roll_head = 0
    for t in nts:
        first_col = [re.sub(r"[\s　 ]+", "", _row_name(r)) for r in t.rows]
        if any("기초" in x for x in first_col) and any("기말" in x for x in first_col):
            roll_first += 1
        head = "".join(c for hr in t.header_rows for c in hr)
        if "기초" in head and "기말" in head:
            roll_head += 1
    d["roll_first"], d["roll_head"] = roll_first, roll_head
    d["balance_warns"] = [w for w in report.warnings if "파싱 오류 가능" in w]
    return d


# ---------- 캐시 전수 실행 ----------


def run_cache_stats(limit: int) -> None:
    type_map: dict[str, tuple[str, str]] = {}
    for name in ("targets.csv", "targets2.csv", "results.csv"):
        p = ROOT / "batch" / name
        if not p.exists():
            continue
        for r in csv.DictReader(p.open(encoding="utf-8-sig")):
            rcp = r.get("rcept_no") or r.get("rcpNo")
            if rcp and rcp not in type_map:
                type_map[rcp] = (r.get("corp_name", ""), r.get("유형", ""))

    pipeline.fetch_html = _offline_fetch  # 캐시 주입 (batch_run 과 동일 기법)

    dirs = sorted(d for d in CACHE_DIR.iterdir() if d.is_dir())
    if limit:
        dirs = dirs[:limit]
    docs: list[dict] = []
    failures: Counter = Counter()
    fail_examples: dict[str, str] = {}
    t0 = time.perf_counter()
    for i, doc_dir in enumerate(dirs, 1):
        rcp = doc_dir.name
        corp, dt = type_map.get(rcp, ("?", "?"))
        _FETCH_LOG.clear()
        t1 = time.perf_counter()
        try:
            report, _data = pipeline.convert_report(rcp)
        except Exception as exc:  # noqa: BLE001
            failures[type(exc).__name__] += 1
            fail_examples.setdefault(type(exc).__name__, f"{rcp} {corp}: {str(exc)[:120]}")
            continue
        d = collect_doc_stats(report, list(_FETCH_LOG), dt)
        d.update({"rcp": rcp, "corp": corp, "sec": time.perf_counter() - t1})
        docs.append(d)
    total_sec = time.perf_counter() - t0

    print(f"### 캐시 전수 오프라인 실행: 디렉터리 {len(dirs)}개, 성공 {len(docs)}건, "
          f"실패 {sum(failures.values())}건, 총 {total_sec:.1f}초 (문서당 평균 {total_sec / max(len(dirs), 1):.2f}초)")
    if failures:
        print("실패 분포:", dict(failures))
        for k, v in fail_examples.items():
            print(f"  예시 [{k}] {v}")

    print("\n[문서 유형 분포]", dict(Counter(d["유형"] for d in docs)))
    print("[스코프 분포]", dict(Counter(d["스코프"] for d in docs)))
    print("[회계기준 분포]", dict(Counter(d["회계기준"] for d in docs)))

    stmt_units = sum((d["stmt_units"] for d in docs), Counter())
    note_units = sum((d["note_units"] for d in docs), Counter())
    print("\n[재무제표 표 단위 분포(표 수)]", dict(stmt_units))
    nt_total = sum(note_units.values())
    print("[주석 표 단위 분포(표 수)]", dict(note_units),
          f"— 추출 실패율 {note_units.get('추출실패(None)', 0) / max(nt_total, 1):.1%}")

    ref_counter = Counter()
    for d in docs:
        rk = d["ref_kind"]
        if not rk:
            ref_counter["없음"] += 1
        else:
            ref_counter[" + ".join(sorted(rk))] += 1
    with_ref = sum(v for k, v in ref_counter.items() if k != "없음")
    print(f"\n[재무상태표·손익계산서 주석참조] 문서 {len(docs)}건 중 참조 있음 {with_ref}건 "
          f"({with_ref / max(len(docs), 1):.1%}) — 방식별 {dict(ref_counter)}")

    rows_total = sum(d["rows_total"] for d in docs)
    rows_deep = sum(d["rows_deep"] for d in docs)
    tables_total = sum(d["tables_total"] for d in docs)
    tables_flat = sum(d["tables_flat"] for d in docs)
    print(f"\n[들여쓰기] 재무제표 본문 행 {rows_total}개 중 depth≥1 행 {rows_deep}개 ({rows_deep / max(rows_total, 1):.1%}) / "
          f"표 {tables_total}개 중 모든 행 depth=0 인 표 {tables_flat}개 ({tables_flat / max(tables_total, 1):.1%})")

    print("\n[총계류 행 빈도 — 재무제표]", dict(sum((d["stmt_total_hits"] for d in docs), Counter())))
    print("[총계류 행 빈도 — 주석 표]", dict(sum((d["note_total_hits"] for d in docs), Counter())))

    bs_prefix_rows = sum(d["bs_prefix_rows"] for d in docs)
    bs_prefix_docs = sum(1 for d in docs if d["bs_prefix_rows"])
    print(f"\n[재무상태표 번호 접두 행] 행 {bs_prefix_rows}개, 문서 {bs_prefix_docs}건 / {len(docs)}건")

    neg = Counter()
    for d in docs:
        for k, v in d["neg"].items():
            neg[k] += 1 if v else 0
    print(f"[음수 표기 변형(원문 HTML, 문서 단위)] {dict(neg)} / {len(docs)}건")

    ncounts = [d["note_count"] for d in docs]
    tcounts = [d["note_table_count"] for d in docs]
    if ncounts:
        print(f"\n[문서당 주석 수] 최소 {min(ncounts)} / 중앙값 {statistics.median(ncounts)} / 최대 {max(ncounts)}")
        print(f"[문서당 주석 표 수] 최소 {min(tcounts)} / 중앙값 {statistics.median(tcounts)} / 최대 {max(tcounts)}")

    roll_first = sum(d["roll_first"] for d in docs)
    roll_head = sum(d["roll_head"] for d in docs)
    print(f"[변동표 후보 주석 표] 첫 열에 기초+기말 모두: {roll_first}개 / 헤더에 기초+기말 모두: {roll_head}개 "
          f"(전체 주석 표 {sum(tcounts)}개)")

    bal = [(d["rcp"], d["corp"], w) for d in docs for w in d["balance_warns"]]
    print(f"\n[대차 불일치 경고 문서] {len(set(b[0] for b in bal))}건")
    for rcp, corp, w in bal:
        print(f"  {rcp} {corp}: {w}")


# ---------- fixture 실물 덤프 ----------

DUMP_SETS = [
    ("사업보고서(연결) — fs_annual_consolidated / notes_annual_consolidated", "연결",
     "fs_annual_consolidated.html", "notes_annual_consolidated.html"),
    ("감사보고서(연결) — fs_audit / notes_audit", "연결", "fs_audit.html", "notes_audit.html"),
    ("별도 감사보고서 — fs_audit_separate / notes_audit_separate", "단일",
     "fs_audit_separate.html", "notes_audit_separate.html"),
]


def _dump_table(t: Table, max_rows: int | None = None) -> None:
    print(f"  caption={t.caption!r} unit={t.unit!r}")
    for i, hr in enumerate(t.header_rows):
        print(f"  header[{i}]={hr!r}")
    rows = t.rows if max_rows is None else t.rows[:max_rows]
    for i, r in enumerate(rows):
        depth = t.depths[i] if i < len(t.depths) else None
        ref = t.note_refs[i] if i < len(t.note_refs) else None
        print(f"  row[{i:02d}] depth={depth} note_ref={ref!r} cells={r!r}")
    if max_rows is not None and len(t.rows) > max_rows:
        print(f"  ... (이하 {len(t.rows) - max_rows}행 생략)")


def dump_fixture(label: str, scope: str, fs_name: str, notes_name: str) -> None:
    print(f"\n===== {label} =====")
    warnings: list[str] = []
    fs_html = (FIXTURES / fs_name).read_text(encoding="utf-8")
    stmts = extract_statements(fs_html, scope, warnings)
    bs = next((s for s in stmts if s.kind == "재무상태표"), None)
    if bs is None:
        print("재무상태표 없음")
    else:
        print(f"[재무상태표] scope={bs.scope} title={bs.title!r} period_text={bs.period_text!r} "
              f"unit={bs.unit!r} basis={bs.basis!r} suffix={bs.suffix!r} 행수={len(bs.table.rows)}")
        _dump_table(bs.table, max_rows=30)

    notes_html = (FIXTURES / notes_name).read_text(encoding="utf-8")
    notes = split_notes(notes_html, scope, warnings)

    def _find(keyword: str) -> Note | None:
        return next((n for n in notes if keyword in n.title.replace(" ", "")), None)

    ppe = _find("유형자산")
    print(f"\n[유형자산 주석] {'없음' if ppe is None else f'주석 {ppe.number}. {ppe.title} (blocks={len(ppe.blocks)})'}")
    if ppe is not None:
        first_table = next((b.table for b in ppe.blocks if b.kind == "table" and b.table is not None), None)
        if first_table is None:
            print("  표 없음")
        else:
            _dump_table(first_table)

    ar = next((n for n in notes if "매출채권" in n.title.replace(" ", "")), None)
    print(f"\n[매출채권 주석] {'없음' if ar is None else f'주석 {ar.number}. {ar.title} (blocks={len(ar.blocks)})'}")
    if ar is not None:
        allow = None
        for b in ar.blocks:
            if b.kind != "table" or b.table is None:
                continue
            cells = [str(c) for r in b.table.rows for c in r] + [c for hr in b.table.header_rows for c in hr]
            if any("대손충당금" in c or "손실충당금" in c for c in cells):
                allow = b.table
                break
        if allow is None:
            print("  대손충당금/손실충당금 표 없음")
        else:
            _dump_table(allow)
    print(f"\n[이 fixture 처리 중 경고 {len(warnings)}건]")
    for w in warnings:
        print(f"  - {w}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows cp949 콘솔 대응
    ap = argparse.ArgumentParser(description="Phase 0 조사")
    ap.add_argument("--limit", type=int, default=0, help="캐시 앞에서 N건만 (0=전체)")
    ap.add_argument("--skip-cache", action="store_true")
    ap.add_argument("--skip-dump", action="store_true")
    args = ap.parse_args()

    if not args.skip_dump:
        print("################ 7. fixture 실물 덤프 ################")
        for label, scope, fs, nt in DUMP_SETS:
            dump_fixture(label, scope, fs, nt)

    if not args.skip_cache:
        print("\n################ 6. 배치 캐시 실측 통계 ################")
        run_cache_stats(args.limit)


if __name__ == "__main__":
    main()
