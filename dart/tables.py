"""HTML table → 2D grid (colspan/rowspan 전개), 숫자 정규화, 단위 추출.

네트워크 호출 없음.

실제 DART HTML 에서 확인한 계정과목 들여쓰기 표현 (2026-09):
    - 사업보고서(DART 편집기 산출물): ``<td>　　현금및현금성자산 (주4,29)</td>`` 처럼
      전각공백(U+3000)을 깊이만큼 반복. 빈 셀도 ``　`` 하나.
    - 감사보고서(회계법인 제출본): ``<td>&nbsp; &nbsp;현금및현금성자산</td>`` 처럼
      ``&nbsp; &nbsp;`` (U+00A0, 공백, U+00A0 = 3자) 를 깊이만큼 반복. 빈 셀은 ``<br/>``.
    - padding-left / text-indent / class 기반 들여쓰기는 없었다.
따라서 depth 는 "strip 전 선두 공백류 문자 수(폭)" 를 구한 뒤, 표 안에서 등장하는
서로 다른 폭을 오름차순으로 순위 매긴 값으로 정한다 (폭 0 → depth 0, 다음 폭 → 1, ...).
"""

from __future__ import annotations

import re
from typing import Optional, Union

from bs4 import Tag

from dart.models import CellValue, Table

# 단위 문구: "(단위 : 원)", "(단위: 백만원)", "(단위 : 천원, 천USD)", "단위:원"
_UNIT_PAREN_RE = re.compile(r"[(（]\s*단위\s*[:：]?\s*([^)）]+?)\s*[)）]")
_UNIT_BARE_RE = re.compile(r"단위\s*[:：]\s*([^\s()（）,、]+(?:\s*[,、]\s*[^\s()（）,、]+)*)")

_LEADING_WS_RE = re.compile(r"^[　  \t]*")
# 계정과목 끝의 주석참조: "(주4,29)", "(주석3, 20)", "(주석 3~5)", "（주4）" → "4,29", "3,20", "3~5", "4"
NOTE_REF_RE = re.compile(r"\s*[(（]\s*(?:주석|주)\s*([\d,\s~\-]+?)\s*[)）]\s*$")
_WS_RE = re.compile(r"[\s　 ]+")

# 전각 → 반각 변환표 (숫자, 쉼표, 괄호, 마침표, 하이픈, 플러스)
_FULLWIDTH_MAP = {ord(c): ord(r) for c, r in zip("０１２３４５６７８９，（）．－＋", "0123456789,().-+")}

NULL_TOKENS: frozenset[str] = frozenset({"", "-", "—", "–", "－", "해당없음", "해당사항없음"})
NEG_PREFIXES: tuple[str, ...] = ("-", "△", "▲")
# 쉼표 없음("1234", "1234.5") 또는 3자리 천단위 쉼표("1,234", "12,345,678.5"). "4,12" 같은 주석 참조는 제외.
_NUMBER_RE = re.compile(r"^(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d*)?$")

Grid = list[list[str]]
DepthGrid = list[list[int]]


def _cell_text(cell: Tag) -> tuple[str, int]:
    """셀 텍스트와 선두 공백 폭을 돌려준다.

    ``<br>`` 은 공백으로 바꾸고, 연속 공백은 하나로 줄이고, 앞뒤를 strip 한다.
    폭은 strip 전 텍스트의 선두 공백류(U+3000, U+00A0, 공백, 탭) 문자 수다.
    중첩 <table> 은 get_text 로 자연히 평탄화된다.
    """
    for br in cell.find_all("br"):
        br.replace_with(" ")
    raw = cell.get_text(" ")
    text = _WS_RE.sub(" ", raw).strip()
    width = len(_LEADING_WS_RE.match(raw).group(0)) if text else 0
    return text, width


def _own_rows(table: Tag) -> list[Tag]:
    """이 표에 직접 속한 <tr> 만 (중첩 표의 tr 제외)."""
    return [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]


def _own_cells(tr: Tag) -> list[Tag]:
    """이 행에 직접 속한 <td>/<th> 만."""
    return [c for c in tr.find_all(["td", "th"]) if c.find_parent("tr") is tr]


def _span(cell: Tag, attr: str) -> int:
    try:
        return max(1, int(str(cell.get(attr, "1")).strip() or 1))
    except ValueError:
        return 1


def count_thead_rows(table: Tag) -> int:
    """<thead> 안의 (셀이 있는) 행 수. 없으면 0."""
    thead = table.find("thead")
    if thead is None or thead.find_parent("table") is not table:
        return 0
    return sum(1 for tr in thead.find_all("tr") if _own_cells(tr))


def html_table_to_grid(
    table: Tag,
    return_depth: bool = False,
    warnings: Optional[list[str]] = None,
    table_no: Optional[int] = None,
) -> Union[Grid, tuple[Grid, DepthGrid]]:
    """<table> 을 colspan/rowspan 을 전개한 직사각형 문자열 그리드로 바꾼다.

    - 병합 셀은 시작 셀의 텍스트를 병합 범위 전체에 복사한다.
    - 모든 행을 최대 열 수로 ``""`` 패딩한다. 행별 열 수가 달라 패딩이 일어나면
      ``warnings`` 에 "표 N: 열 수 불일치(최대 X, 최소 Y)로 패딩" 을 기록한다.
    - ``<td>``/``<th>`` 가 하나도 없는 ``<tr>`` 은 건너뛴다.
    - 중첩 <table> 은 바깥만 처리하고 안쪽은 텍스트로 평탄화한다.
    - depth 는 표 안에서 등장한 선두 공백 폭을 오름차순 순위로 바꾼 값이다 (모듈 docstring 참고).

    Args:
        table: BeautifulSoup <table> 태그. (br 치환을 위해 in-place 수정된다.)
        return_depth: True 면 ``(grid, depth_grid)`` 를 돌려준다.
        warnings: 경고 누적 리스트. None 이면 기록하지 않는다.
        table_no: 경고 메시지에 쓸 표 순번. None 이면 "?".

    Returns:
        그리드 또는 ``(grid, depth_grid)``.
    """
    grid: list[list[str]] = []
    widths: list[list[int]] = []
    carry: dict[int, tuple[int, str, int]] = {}  # col -> (남은 행 수, text, width)

    for tr in _own_rows(table):
        cells = _own_cells(tr)
        if not cells:
            continue
        row: dict[int, tuple[str, int]] = {}
        col = 0

        def _consume_carry() -> None:
            """현재 col 부터 이어지는 rowspan 이월 셀을 채우고 col 을 전진시킨다."""
            nonlocal col
            while col in carry:
                remain, text, w = carry.pop(col)
                row[col] = (text, w)
                if remain > 1:
                    carry[col] = (remain - 1, text, w)
                col += 1

        for cell in cells:
            _consume_carry()
            text, w = _cell_text(cell)
            rs, cs = _span(cell, "rowspan"), _span(cell, "colspan")
            for k in range(cs):
                row[col + k] = (text, w)
                if rs > 1:
                    carry[col + k] = (rs - 1, text, w)
            col += cs
        _consume_carry()  # 행 끝 이후에도 이어지는 rowspan 셀
        n = max(row) + 1 if row else 0
        grid.append([row.get(i, ("", 0))[0] for i in range(n)])
        widths.append([row.get(i, ("", 0))[1] for i in range(n)])

    ncols = max((len(r) for r in grid), default=0)
    min_cols = min((len(r) for r in grid), default=0)
    if warnings is not None and grid and min_cols != ncols:
        warnings.append(f"표 {table_no if table_no is not None else '?'}: 열 수 불일치(최대 {ncols}, 최소 {min_cols})로 패딩")
    for r, w in zip(grid, widths):
        r.extend([""] * (ncols - len(r)))
        w.extend([0] * (ncols - len(w)))

    if not return_depth:
        return grid
    # 순위는 텍스트가 있는 셀의 폭만으로 매긴다 (빈 셀은 폭 0 으로 취급됨). 폭 0 은 항상 depth 0.
    distinct = sorted({w for row, trow in zip(widths, grid) for w, t in zip(row, trow) if t} | {0})
    rank = {w: i for i, w in enumerate(distinct)}
    depth_grid = [[rank.get(w, 0) for w in row] for row in widths]
    return grid, depth_grid


def normalize_number(text: str) -> CellValue:
    """금액 문자열을 숫자로 정규화한다.

    - 음수: ``(1,234)``, ``-1,234``, ``△1,234``, ``▲1,234``, ``(1,234.5)`` → 음수 float
    - 양수: ``1,234``, ``1234``, ``1,234.5``, ``1,234.`` → float. ``0`` → 0.0
    - 빈 값: ``-``, ``—``, ``－``, 빈 문자열, ``해당없음``, ``해당사항없음`` → None
    - 전각 숫자·쉼표·괄호·마침표·하이픈은 반각으로 바꾼 뒤 처리한다.
    - 파싱 불가: 원문 str 그대로 반환 (텍스트 손실 방지).

    Args:
        text: 셀 텍스트.

    Returns:
        float | None | str.
    """
    if text is None:
        return None
    s = str(text).translate(_FULLWIDTH_MAP)
    s = _WS_RE.sub("", s)
    if s in NULL_TOKENS:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        s, neg = s[1:-1].strip(), True
    elif s.startswith(NEG_PREFIXES):
        s, neg = s[1:].strip(), True
    elif s.startswith("+"):
        s = s[1:]
    if not _NUMBER_RE.match(s):
        return text
    digits = s.replace(",", "")
    value = float(digits.rstrip(".") or "0")
    return -value if neg else value


def extract_unit(text: str) -> Optional[str]:
    """``(단위 : 원)``, ``(단위: 백만원)``, ``(단위 : 천원, 천USD)``, ``단위:원`` 에서 단위 문자열을 뽑는다.

    괄호 유무, 콜론 종류(``:`` ``：``), 공백을 모두 허용한다. 괄호형을 우선 찾고,
    없으면 콜론이 있는 괄호 없는 형태를 찾는다.

    Args:
        text: 검색할 텍스트.

    Returns:
        단위 문자열 (예: ``"원"``, ``"천원, 천USD"``). 없으면 None.
    """
    if not text:
        return None
    m = _UNIT_PAREN_RE.search(text) or _UNIT_BARE_RE.search(text)
    if not m:
        return None
    unit = _WS_RE.sub(" ", m.group(1)).strip(" :：")
    return unit or None


def _is_numeric_cell(text: str) -> bool:
    return isinstance(normalize_number(text), float)


def split_header_rows(grid: Grid, thead_rows: Optional[int] = None) -> int:
    """헤더 행 수를 정한다.

    ``thead_rows`` (:func:`count_thead_rows` 결과) 가 양수면 그대로 쓴다. 아니면
    "숫자 셀이 하나도 없고, 첫 열 외에 비어 있지 않은 셀이 있는 선두 연속 행" 수를 헤더로 본다
    (최소 1, 최대 3).

    Args:
        grid: :func:`html_table_to_grid` 결과.
        thead_rows: <thead> 행 수. None 또는 0 이면 휴리스틱.

    Returns:
        헤더 행 수.
    """
    if thead_rows:
        return min(thead_rows, len(grid))
    if not grid:
        return 0
    count = 0
    for row in grid[:3]:
        has_number = any(_is_numeric_cell(c) for c in row[1:])
        has_label = any(c.strip() for c in row[1:])
        if has_number or (count > 0 and not has_label):
            break
        count += 1
    return max(1, min(count, 3, len(grid)))


def grid_to_table(
    grid: Grid,
    depth_grid: Optional[DepthGrid],
    header_count: int,
    caption: Optional[str],
    unit: Optional[str],
    warnings: list[str],
) -> Table:
    """문자열 그리드를 :class:`Table` 로 변환한다.

    - 헤더 행은 문자열 그대로 ``header_rows`` 에.
    - 본문 행의 첫 열(계정과목)은 문자열 + depth(``depths``). 헤더 셀이 ``주석`` 인 열은 문자열 유지
      (``4,12`` 를 숫자로 오인하지 않기 위함). 그 외 열은 :func:`normalize_number`.
    - 전체 폭 colspan 구분 행(모든 셀이 첫 셀과 동일, 예: ``총포괄손익:``)은 첫 열만 남기고 나머지는 None.
    - 첫 열 끝의 ``(주4,29)`` 는 :func:`split_note_ref` 로 떼어 ``note_refs`` 에 넣는다.
    - 행 길이가 다르면 조용히 패딩한다 (경고는 :func:`html_table_to_grid` 가 원본 기준으로 남긴다).
      숫자(float)와 문자열이 실제로 섞인 열만 ``warnings`` 에 기록한다 (전부 문자열인 열은 텍스트 열).

    Args:
        grid: 문자열 그리드.
        depth_grid: 그리드와 같은 모양의 depth. None 이면 모두 0.
        header_count: 헤더 행 수.
        caption: 표 위 캡션.
        unit: 단위 문구.
        warnings: 경고 누적 리스트 (in-place append).

    Returns:
        변환된 Table.
    """
    label = caption or "(캡션 없음)"
    ncols = max((len(r) for r in grid), default=0)
    grid = [r + [""] * (ncols - len(r)) for r in grid]
    if depth_grid is None:
        depth_grid = [[0] * ncols for _ in grid]
    else:
        depth_grid = [d + [0] * (ncols - len(d)) for d in depth_grid]

    header_count = max(0, min(header_count, len(grid)))
    header_rows = [list(r) for r in grid[:header_count]]
    note_cols = {
        i for hr in header_rows for i, c in enumerate(hr) if _WS_RE.sub("", c) == "주석"
    }

    rows: list[list[CellValue]] = []
    depths: list[int] = []
    note_refs: list[Optional[str]] = []
    str_count: dict[int, int] = {}
    num_count: dict[int, int] = {}
    for r, d in zip(grid[header_count:], depth_grid[header_count:]):
        out: list[CellValue] = []
        # 전체 폭 colspan 구분 행("총포괄손익:" 등): 모든 셀이 첫 셀과 같으면 첫 열만 남기고 나머지는 None.
        # 부분 colspan 텍스트 행("(*)" | "각주 문장" 반복): 2열 이후가 모두 같은 비숫자 텍스트면 2열에만 남긴다.
        section_row = ncols > 1 and bool(r and r[0].strip()) and all(c == r[0] for c in r[1:])
        span_text_row = (
            not section_row and ncols > 2 and bool(r[1].strip()) and all(c == r[1] for c in r[2:])
            and isinstance(normalize_number(r[1]), str)
        )
        ref: Optional[str] = None
        for i, cell in enumerate(r):
            if i == 0:
                name, ref = split_note_ref(cell)
                out.append(name)
                continue
            if i in note_cols and not section_row:
                out.append(cell)
                continue
            if section_row or (span_text_row and i >= 2):
                out.append(None)
                continue
            if span_text_row and i == 1:
                out.append(cell)
                continue
            v = normalize_number(cell)
            if isinstance(v, str) and v.strip():
                str_count[i] = str_count.get(i, 0) + 1
            elif isinstance(v, float):
                num_count[i] = num_count.get(i, 0) + 1
            out.append(v)
        rows.append(out)
        depths.append(d[0] if d else 0)
        note_refs.append(ref)

    # 숫자와 문자열이 실제로 섞인 열만 경고한다 (전부 문자열인 열은 텍스트 열이므로 정상).
    for col, n in sorted(str_count.items()):
        if num_count.get(col, 0) == 0:
            continue
        head = " / ".join(hr[col] for hr in header_rows if col < len(hr) and hr[col]) or f"{col + 1}열"
        warnings.append(f"[{label}] 숫자 열 '{head}' 에 숫자로 읽지 못한 값 {n}개가 문자열로 남았습니다.")

    return Table(caption=caption, unit=unit, header_rows=header_rows, rows=rows, depths=depths, note_refs=note_refs)


def split_note_ref(name: str) -> tuple[str, Optional[str]]:
    """계정과목 끝의 ``(주4,29)``/``(주석3, 20)``/``(주석 3~5)``/``（주4）`` 를 떼어 ``("계정과목", "4,29")`` 로 나눈다.

    없으면 ``(원문, None)``. 추출값은 공백을 제거해 ``3,20`` 처럼 정규화한다.

    Args:
        name: 계정과목 텍스트.

    Returns:
        ``(계정과목, 주석참조)``. 주석참조는 공백을 제거한 ``"4,29"`` 형태.
    """
    if not isinstance(name, str):
        return name, None
    m = NOTE_REF_RE.search(name)
    if not m:
        return name, None
    return name[: m.start()].rstrip(), re.sub(r"\s+", "", m.group(1)).strip(",~-") or None
