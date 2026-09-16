"""재무제표 4종(+포괄손익계산서) 식별 및 추출.

네트워크 호출 없음. viewer.do 본문 HTML 문자열만 받는다.

실측 구조 (2026-09):
    - 사업보고서: ``<p class="table-group-xbrl">2-1. 연결 재무상태표</p>`` →
      ``<table class="nb">`` 제목표(제목 / 기간 3행 / ``(단위 : 백만원)``) → ``<table border="1">`` 본문표(<thead>).
    - 감사보고서: ``<table class="nb">`` 2열 제목표(``연 결 재 무 상 태 표`` / 기간 / 회사명 / ``(단위: 원)``) →
      본문표 → ``<table class="nb">`` "별첨 주석은 본 연결재무제표의 일부입니다." 반복.
      주석은 ``<p class="section-2"><a name="toc2">주석</a></p>`` 블록에서 시작하고 그 뒤 ``1. 일반 사항`` 문단이 온다.
    - 두 경우 모두 블록은 <body> 바로 아래에 평탄하게 놓인다.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import replace
from typing import Iterator, Optional

from bs4 import BeautifulSoup, Tag

from dart.models import Scope, Statement, StatementKind, Table
from dart.notes import NOTE_HEADING_RE
from dart.tables import (
    count_thead_rows,
    extract_unit,
    grid_to_table,
    html_table_to_grid,
    normalize_number,
    split_header_rows,
)
from dart.tree import normalize_title

logger = logging.getLogger(__name__)

# 제목 매칭 우선순위 (정규화 제목에 포함되면 해당 kind). 포괄손익 → 손익 순서 중요.
KIND_KEYWORDS: list[tuple[str, StatementKind]] = [
    ("포괄손익계산서", "포괄손익계산서"),
    ("손익계산서", "손익계산서"),
    ("재무상태표", "재무상태표"),
    ("자본변동표", "자본변동표"),
    ("현금흐름표", "현금흐름표"),
]

BLOCK_TAGS: frozenset[str] = frozenset({"table", "p", "div", "h1", "h2", "h3", "h4", "h5", "h6"})
CTX_SIZE = 8
BODY_MIN_COLS = 3
BODY_NUMERIC_RATIO = 0.2

# 절단: 표 밖 블록의 정규화(공백·점 제거) 텍스트가 정확히 이것이면 주석 시작
NOTE_START_TEXTS: frozenset[str] = frozenset(
    {"주석", "재무제표에대한주석", "연결재무제표에대한주석", "재무제표주석", "연결재무제표주석"}
)

_WS_RE = re.compile(r"[\s　 ]+")
_PERIOD_RE = re.compile(r"제\s*\d+\s*[(（]?[^)）]*[)）]?\s*기|현재|부터|까지|\d{4}\s*[.년]\s*\d{1,2}")


def _clean(text: str) -> str:
    """공백류를 하나의 공백으로 줄이고 앞뒤를 자른다."""
    return _WS_RE.sub(" ", text or "").strip()


def _compact(text: str) -> str:
    """공백·점 제거 (절단 규칙 비교용. 번호 접두는 유지)."""
    return _WS_RE.sub("", text or "").replace(".", "")


def _iter_blocks(soup: BeautifulSoup) -> Iterator[Tag]:
    """본문 블록을 문서 순서대로 낸다. 최상위 <table> 과, 표 밖의 <p>/<h1~6> 만 낸다.

    <div> 는 컨테이너로 보고 직접 내지 않는다 (그 안의 p/table 이 순회된다).
    """
    root = soup.body or soup
    for el in root.descendants:
        name = getattr(el, "name", None)
        if name not in BLOCK_TAGS or name == "div":
            continue
        if el.find_parent("table") is not None:
            continue
        yield el


def _table_shape(table: Tag) -> tuple[int, int, float]:
    """(행 수, 최대 열 수, 숫자 셀 비율)."""
    rows = [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]
    ncols, total, numeric = 0, 0, 0
    for tr in rows:
        cells = [c for c in tr.find_all(["td", "th"]) if c.find_parent("tr") is tr]
        ncols = max(ncols, len(cells))
        for c in cells:
            total += 1
            if isinstance(normalize_number(_clean(c.get_text(" "))), float):
                numeric += 1
    return len(rows), ncols, (numeric / total if total else 0.0)


def _is_body_table(table: Tag) -> bool:
    """본문표 판정: <thead> 가 있거나, (class="nb" 가 아니면서) 열 ≥ 3 이고 숫자 셀 20% 이상."""
    if count_thead_rows(table) > 0:
        return True
    if "nb" in (table.get("class") or []):
        return False
    _, ncols, ratio = _table_shape(table)
    return ncols >= BODY_MIN_COLS and ratio >= BODY_NUMERIC_RATIO


def _table_cell_texts(table: Tag) -> list[str]:
    """제목표의 셀 텍스트를 문서 순서대로 (연속 중복 제거, 빈 셀 제외)."""
    out: list[str] = []
    for c in table.find_all(["td", "th"]):
        t = _clean(c.get_text(" "))
        if t and (not out or out[-1] != t):
            out.append(t)
    return out


def _kind_from_title(text: str) -> Optional[StatementKind]:
    t = normalize_title(text)
    for kw, kind in KIND_KEYWORDS:
        if kw in t:
            return kind
    return None


def _kind_from_content(table: Table) -> Optional[StatementKind]:
    """표 내용으로 종류 판정.

    판정 순서는 재무상태표 → 자본변동표 → 현금흐름표 → 포괄손익계산서 → 손익계산서 로 고정한다.
    이유: 현금흐름표 첫 열에는 ``당기순이익`` 이, 포괄손익계산서에도 ``당기순이익`` 이 나오므로
    손익계산서 조건(``영업이익``|``당기순이익``)을 먼저 보면 오판한다. 반대로 재무상태표(``자산총계``&``부채총계``),
    자본변동표(헤더 ``자본금``&잉여금/결손금), 현금흐름표(``영업활동``&``현금흐름``) 조건은 다른 표에 나타나지 않아
    앞에 두어도 안전하다.
    """
    names = "".join(_compact(str(r[0])) for r in table.rows if r)
    header = "".join(_compact(c) for hr in table.header_rows for c in hr)
    if "자산총계" in names and "부채총계" in names:
        return "재무상태표"
    if "자본금" in header and ("이익잉여금" in header or "결손금" in header):
        return "자본변동표"
    if "영업활동" in names and "현금흐름" in names:
        return "현금흐름표"
    if "총포괄" in names or "기타포괄" in names:
        return "포괄손익계산서"
    if "영업이익" in names or "당기순이익" in names:
        return "손익계산서"
    return None


def identify_statement_kind(
    title_ctx: list[str], table: Optional[Table] = None
) -> tuple[Optional[StatementKind], str]:
    """재무제표 종류를 판정한다.

    1. ``title_ctx`` 를 뒤에서부터 보며 정규화(공백·전각공백·번호접두 제거) 후
       포괄손익계산서 > 손익계산서 > 재무상태표 > 자본변동표 > 현금흐름표 순으로 첫 매칭. basis="제목".
    2. 없으면 표 내용으로 판정. basis="내용".
    3. 둘 다 아니면 ``(None, "")``.

    Args:
        title_ctx: 표 앞 블록 텍스트 목록 (오래된 것 → 최근 순).
        table: 변환된 표 (내용 기반 판정용). None 이면 제목만 본다.

    Returns:
        ``(kind, basis)``.
    """
    for text in reversed(title_ctx):
        kind = _kind_from_title(text)
        if kind:
            return kind, "제목"
    if table is not None:
        kind = _kind_from_content(table)
        if kind:
            return kind, "내용"
    return None, ""


def _title_index(title_ctx: list[str]) -> int:
    """제목으로 매칭된 ctx 인덱스 (뒤에서부터 첫 매칭). 없으면 -1."""
    for i in range(len(title_ctx) - 1, -1, -1):
        if _kind_from_title(title_ctx[i]):
            return i
    return -1


def _period_and_unit(title_ctx: list[str], title_idx: int) -> tuple[Optional[str], Optional[str]]:
    """제목 이후(제목이 없으면 ctx 전체) 텍스트에서 기간 문구와 단위를 뽑는다."""
    after = title_ctx[title_idx + 1 :] if title_idx >= 0 else list(title_ctx)
    periods: list[str] = []
    unit: Optional[str] = None
    for text in after:
        u = extract_unit(text)
        if u and unit is None:
            unit = u
        stripped = re.sub(r"[(（]\s*단위[^)）]*[)）]", "", text).strip()
        if stripped and _PERIOD_RE.search(stripped) and stripped not in periods:
            periods.append(stripped)
    if unit is None:
        for text in reversed(title_ctx[: title_idx + 1] if title_idx >= 0 else []):
            unit = extract_unit(text)
            if unit:
                break
    return (" / ".join(periods) or None), unit


def merge_split_statement(a: Statement, b: Statement) -> Statement:
    """헤더가 완전히 같은 두 재무제표(페이지 분할 등)를 하나로 합친다. rows/depths 를 이어붙인다.

    Args:
        a: 앞 표.
        b: 뒤 표 (헤더가 ``a`` 와 같아야 한다).

    Returns:
        합쳐진 새 Statement (``a`` 의 메타데이터 유지).

    Raises:
        ValueError: 헤더가 다른 경우.
    """
    if a.table.header_rows != b.table.header_rows:
        raise ValueError("헤더가 달라 병합할 수 없습니다.")
    table = replace(
        a.table,
        rows=list(a.table.rows) + list(b.table.rows),
        depths=list(a.table.depths) + list(b.table.depths),
    )
    return replace(a, table=table)


_NAME_NORM_RE = re.compile(r"[(（][^)）]*[)）]")
_TOTAL_LE_KEYS = ("부채와자본총계", "부채및자본총계", "자본과부채총계", "부채와자본의총계")
# XBRL 표준계정명 문서(예: 20260316001287 현대리바트)는 총계 행이 "자산"/"부채"/"자본"/"자본과 부채".
_FALLBACK_LE_KEYS = ("자본과부채", "부채와자본", "부채및자본")


def _norm_account(name: object) -> str:
    return _WS_RE.sub("", _NAME_NORM_RE.sub("", str(name or "")))


def _fallback_total_rows(stmt: Statement) -> dict[str, list]:
    """XBRL 표준계정명 fallback 행: 이름이 정확히 자산/부채/자본/자본과부채류이고 숫자 값이 하나 이상인 행.

    헤더성 ``자산`` 행은 값이 없어 제외된다. 같은 이름이 여러 번이면 마지막 것 (총계는 맨 끝에 온다).
    """
    out: dict[str, list] = {}
    for r in stmt.table.rows:
        key = _norm_account(r[0] if r else "")
        if key in ("자산", "부채", "자본", *_FALLBACK_LE_KEYS) and any(isinstance(v, float) for v in r[1:]):
            out[key] = r
    return out


def validate_balance_sheet(stmt: Statement, warnings: list[str]) -> None:
    """재무상태표 대차 검증: 열마다 ``자산총계 == 부채총계 + 자본총계`` (또는 ``부채와자본총계``) 를 확인한다.

    계정과목은 공백·괄호내용을 제거해 비교한다. 차이가 ``max(1, 자산총계×1e-6)`` 를 넘으면
    "파싱 오류 가능" 경고, 필요한 행을 못 찾으면 "대차 검증 불가" 경고를 ``warnings`` 에 남긴다.
    ``~총계`` 행이 없으면 XBRL 표준계정명(``자산``/``부채``/``자본``/``자본과 부채``, 숫자 값이 있는 행)
    으로 fallback 한다 (경고 아님, logging.info).
    주석 열(헤더 ``주석``)과 자산총계가 None 인 열은 건너뛴다. 현금흐름표는 검증하지 않는다.

    Args:
        stmt: 재무상태표 Statement.
        warnings: 경고 누적 리스트.
    """
    rows: dict[str, list] = {}
    for r in stmt.table.rows:
        key = _norm_account(r[0] if r else "")
        if key in ("자산총계", "부채총계", "자본총계", *_TOTAL_LE_KEYS) and key not in rows:
            rows[key] = r
    assets, liab, equity = rows.get("자산총계"), rows.get("부채총계"), rows.get("자본총계")
    total_le = next((rows[k] for k in _TOTAL_LE_KEYS if k in rows), None)
    if assets is None or ((liab is None or equity is None) and total_le is None):
        # XBRL 표준계정명 fallback: "자산"/"부채"/"자본"/"자본과 부채" 정확 일치 + 숫자 값 행
        fb = _fallback_total_rows(stmt)
        used: list[str] = []
        for name, current in (("자산", assets), ("부채", liab), ("자본", equity)):
            if current is None and name in fb:
                used.append(name)
        assets = assets if assets is not None else fb.get("자산")
        liab = liab if liab is not None else fb.get("부채")
        equity = equity if equity is not None else fb.get("자본")
        if (liab is None or equity is None) and total_le is None:
            le_key = next((k for k in _FALLBACK_LE_KEYS if k in fb), None)
            if le_key:
                total_le = fb[le_key]
                used.append(le_key)
        if used:
            logger.info("재무상태표(%s) 총계 행 fallback 사용(XBRL 표준계정명): %s", stmt.scope, ", ".join(used))
    missing = [n for n, v in (("자산총계", assets), ("부채총계", liab), ("자본총계", equity)) if v is None]
    if assets is None or (missing and total_le is None):
        warnings.append(f"재무상태표({stmt.scope}) 대차 검증 불가: {', '.join(missing)} 행 없음")
        return
    header = stmt.table.header_rows
    note_cols = {i for hr in header for i, c in enumerate(hr) if _WS_RE.sub("", c) == "주석"}

    def num(row, col: int) -> Optional[float]:
        return row[col] if row is not None and col < len(row) and isinstance(row[col], float) else None

    for col in range(1, len(assets)):
        a = num(assets, col)
        if col in note_cols or a is None:
            continue
        if liab is not None and equity is not None:
            l_val, e_val = num(liab, col) or 0.0, num(equity, col) or 0.0
            expected, desc = l_val + e_val, f"부채 {l_val:,.0f} + 자본 {e_val:,.0f}"
        else:
            t = num(total_le, col)
            if t is None:
                continue
            expected, desc = t, f"부채와자본총계 {t:,.0f}"
        diff = abs(a - expected)
        if diff > max(1.0, abs(a) * 1e-6):
            head = " / ".join(hr[col] for hr in header if col < len(hr) and hr[col]) or f"{col + 1}열"
            warnings.append(f"재무상태표({stmt.scope}) {head}: 자산총계 {a:,.0f} ≠ {desc} (차이 {diff:,.0f}) — 파싱 오류 가능")


def _is_note_start(text: str) -> bool:
    if _compact(text) in NOTE_START_TEXTS:
        return True
    m = NOTE_HEADING_RE.match(text)
    return bool(m and int(m.group(1)) == 1)


def extract_statements(html: str, scope: Scope, warnings: list[str]) -> list[Statement]:
    """재무제표 본문 HTML 에서 재무제표 표들을 찾아 :class:`Statement` 목록으로 만든다.

    블록을 순서대로 돌며 최근 8개 블록 텍스트(``pending_title_ctx``)를 유지한다. 본문표를 만나면
    ctx 로 제목 판정(없으면 내용 판정)하고, 제목표(``class="nb"``)·문단은 ctx 에 넣는다.
    재무제표를 하나라도 찾은 뒤 표 밖 블록이 주석 시작(``주석`` 단독 표제 또는 ``1. ...`` 제목)이면
    순회를 끝낸다 (감사보고서는 재무제표 노드 응답에 주석 본문이 포함되기 때문).
    같은 kind 가 반복되면 헤더가 같을 때 병합, 다르면 ``suffix`` 에 ``_2``, ``_3`` 을 붙인다.

    Args:
        html: viewer.do 본문 HTML.
        scope: "연결" | "별도" | "단일".
        warnings: 경고 누적 리스트 (in-place append).

    Returns:
        원문 순서대로의 Statement 목록.
    """
    soup = BeautifulSoup(html, "lxml")
    ctx: deque[str] = deque(maxlen=CTX_SIZE)
    results: list[Statement] = []
    found_any = False
    body_table_no = 0
    block_no = 0
    remaining_tables = 0
    cut_at: Optional[tuple[int, str]] = None

    for block in _iter_blocks(soup):
        block_no += 1
        if cut_at is not None:
            if block.name == "table":
                remaining_tables += 1
            continue

        if block.name != "table":
            text = _clean(block.get_text(" "))
            if found_any and text and _is_note_start(text):
                cut_at = (block_no, text)
                continue
            if text:
                ctx.append(text)
            continue

        if not _is_body_table(block):
            ctx.extend(_table_cell_texts(block))
            continue

        body_table_no += 1
        grid, depth_grid = html_table_to_grid(block, return_depth=True, warnings=warnings, table_no=body_table_no)
        header_count = split_header_rows(grid, count_thead_rows(block))
        ctx_list = list(ctx)
        title_idx = _title_index(ctx_list)
        title = _clean(ctx_list[title_idx]) if title_idx >= 0 else None
        period_text, unit = _period_and_unit(ctx_list, title_idx)
        table = grid_to_table(grid, depth_grid, header_count, title, unit, warnings)
        kind, basis = identify_statement_kind(ctx_list, table)
        ctx.clear()
        if kind is None:
            logger.debug("본문표 %d: 재무제표로 식별되지 않아 무시 (ctx=%r)", body_table_no, ctx_list[-3:])
            continue
        if basis == "내용":
            warnings.append(f"제목 없이 내용 기반 분류: {kind}, 표 순번 {body_table_no}")
            title = title or kind
        stmt = Statement(
            kind=kind, scope=scope, title=title or kind, period_text=period_text, unit=unit,
            table=table, basis=basis,
        )
        found_any = True

        same = [s for s in results if s.kind == kind]
        if same:
            prev = same[-1]
            if prev.table.header_rows == stmt.table.header_rows:
                results[results.index(prev)] = merge_split_statement(prev, stmt)
                warnings.append(f"{kind}{prev.suffix}: 헤더가 같은 표 {body_table_no}를 병합")
                continue
            stmt.suffix = f"_{len(same) + 1}"
            warnings.append(f"{kind}: 같은 종류의 표가 다시 나와 suffix {stmt.suffix!r} 부여 (표 순번 {body_table_no})")
        results.append(stmt)

    if cut_at is not None:
        logger.info("블록 %d에서 주석 시작 감지(%r), 이후 표 %d개 미처리", cut_at[0], cut_at[1], remaining_tables)
    for stmt in results:
        if stmt.kind == "재무상태표":
            validate_balance_sheet(stmt, warnings)
    return results
