"""주석 영역을 주석번호 단위로 분할.

네트워크 호출 없음. viewer.do 주석 본문 HTML 문자열만 받는다.

실측 구조 (2026-09):
    - 사업보고서: 제목은 ``<p class="table-group-xbrl"><a name="tocN">1. 일반적 사항 (연결)</a></p>``.
      본문은 ``<table class="nb">`` 1×1 레이아웃 표 안의 ``<p>`` 들이며, 데이터 표는 최상위
      ``<table border="1">`` 또는 레이아웃 표 셀 안에 중첩된다. 문단 구분은 ``<br/><br/>``.
    - 감사보고서: 제목은 ``<p><span bookmarktext="1. 일반 사항" id="bookmark_1">1. 일반 사항</span></p>``.
      본문 ``<p>`` 와 데이터 표가 <body> 바로 아래 평탄하게 놓인다. ``class="nb"`` 표는 기간 문구·
      ``(주1) ...`` 각주 같은 레이아웃용이다.
제목 검출은 3단계: 구조 신호(bookmarktext, table-group-xbrl) → 텍스트 정규식(빈 구간만) → 순차성 검증.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterator, Optional, Union

from bs4 import BeautifulSoup, NavigableString, Tag

from dart.models import Note, NoteBlock, Scope
from dart.tables import count_thead_rows, extract_unit, grid_to_table, html_table_to_grid, split_header_rows

logger = logging.getLogger(__name__)

# "1. 일반 사항", "21. 우발부채와 약정사항", 가지 번호 "14-1. 무형자산" (실측 20260316001287).
# "2.1 ..." 은 (?!\d) 로 제외. group: (번호, 가지번호|None, 제목).
NOTE_HEADING_RE = re.compile(r"^\s*(\d{1,2})(?:-(\d{1,2}))?\s*[.．]\s*(?!\d)(\S.*)$")
_NUMBER_ONLY_RE = re.compile(r"^\s*\d{1,2}(?:-\d{1,2})?\s*[.．]\s*$")
# 하위 항목: 2.1 / (1) / 가. / ① / 2-1
SUBHEADING_RE = re.compile(r"^(\d{1,2}\.\d|\(\d+\)|[가-힣]\.|[①-⑳]|\d{1,2}-\d)")
_UNIT_PHRASE_RE = re.compile(r"[(（]?\s*단위\s*[:：]?\s*[^)）]*[)）]?")
_WS_RE = re.compile(r"[\s　 ]+")
_PARA_SPLIT_RE = re.compile(r"\n[ \t　 ]*\n")

BOOKMARK_ATTR_LIMIT = 20  # DART 편집기가 bookmarktext 속성을 자르는 길이 (실측)
TITLE_MAX_LEN = 80
SUBHEADING_MAX_LEN = 60
TITLE_BAD_ENDINGS = (".", "다", "음", "임")
BLOCK_TAGS = ("p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol")
STRUCTURE_P_CLASS = "table-group-xbrl"


@dataclass
class _Block:
    """평탄화된 본문 블록. kind: "p" | "table"."""

    kind: str
    text: str = ""
    tag: Optional[Tag] = None
    structure_title: Optional[str] = None  # 구조 신호로 얻은 제목 텍스트 (bookmarktext 등)
    is_section: bool = False  # <p class="section-N"> 등 절 제목 (주석 제목 아님)
    maybe_truncated: bool = False  # bookmarktext 가 정확히 20자이고 내부 텍스트가 비어 제목이 잘렸을 수 있음


@dataclass
class _Candidate:
    index: int  # 블록 인덱스
    number: int
    title: str
    source: str  # "structure" | "text"
    span: int = 1  # 제목이 차지한 블록 수 (분할 제목이면 2)
    maybe_truncated: bool = False
    branch: Optional[int] = None  # 가지 번호 (14-1 의 1)


@dataclass
class _NoteAcc:
    number: int
    title: str
    source: str
    blocks: list[NoteBlock] = field(default_factory=list)
    mixed_tables: int = 0  # 숫자 열에 문자열이 섞인 표 수 (주석당 1건으로 경고 집계)
    branch: Optional[int] = None


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def is_note_heading(text: str) -> Optional[tuple[int, str]]:
    """텍스트가 최상위 주석 제목 형식(``N. 제목``)이면 ``(번호, 제목)``. 하위 번호(``2.1``)는 None.

    Args:
        text: 문단 텍스트.

    Returns:
        ``(number, title)`` 또는 None.
    """
    h = parse_heading(text)
    return (h[0], h[2]) if h else None


def parse_heading(text: str) -> Optional[tuple[int, Optional[int], str]]:
    """``N. 제목`` / ``N-M. 제목`` 이면 ``(번호, 가지번호|None, 제목)``. 아니면 None.

    Args:
        text: 문단 텍스트.

    Returns:
        ``(number, branch, title)`` 또는 None.
    """
    m = NOTE_HEADING_RE.match(_clean(text))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)) if m.group(2) else None, m.group(3).strip()


def _label(number: int, branch: Optional[int]) -> str:
    """주석 번호 표기: 14 또는 14-1."""
    return f"{number}-{branch}" if branch else str(number)


def is_subheading(text: str) -> bool:
    """``2.1``, ``(1)``, ``가.``, ``①``, ``2-1`` 로 시작하고 60자 이하이면 하위 제목.

    Args:
        text: 문단 텍스트.

    Returns:
        하위 제목이면 True.
    """
    t = _clean(text)
    return bool(t) and len(t) <= SUBHEADING_MAX_LEN and SUBHEADING_RE.match(t) is not None


# ---------- 평탄화 ----------


def _own_rows(table: Tag) -> list[Tag]:
    return [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]


def _own_cells(tr: Tag) -> list[Tag]:
    return [c for c in tr.find_all(["td", "th"]) if c.find_parent("tr") is tr]


def _is_layout_table(table: Tag) -> bool:
    """``class="nb"`` 이고 자기 <thead> 가 없고 열 ≤ 2 이면 레이아웃(컨테이너) 표."""
    if "nb" not in (table.get("class") or []):
        return False
    if count_thead_rows(table) > 0:
        return False
    ncols = max((len(_own_cells(r)) for r in _own_rows(table)), default=0)
    return ncols <= 2


def _is_title_line(line: str) -> bool:
    """한 줄이 텍스트 단계 주석 제목 조건(``N. 제목``, 길이 ≤ 80, 문장 종결로 끝나지 않음)을 만족하는지."""
    t = _clean(line)
    m = NOTE_HEADING_RE.match(t)
    return bool(m) and len(t) <= TITLE_MAX_LEN and not m.group(3).strip().endswith(TITLE_BAD_ENDINGS)


# 제목 줄 뒤에서 별도 블록으로 떼는 하위 번호 줄: (1) / 2.1 / 가. / ①  (길이 ≤ 60)
_SUB_LINE_RE = re.compile(r"^(\(\d+\)|\d{1,2}\.\d|[가-힣]\.|[①-⑳])")


def _is_sub_line(line: str) -> bool:
    return len(line) <= SUBHEADING_MAX_LEN and _SUB_LINE_RE.match(line) is not None


def _split_heading_lines(paragraph: str) -> list[str]:
    """문단 안의 줄(<br> 하나로 나뉜 줄) 중 제목 조건을 만족하는 줄이 있으면 그 앞·제목·뒤로 쪼갠다.

    ``<p>회사명 : X<br/>1. 일반사항<br/>(1) 개요<br/>본문</p>`` → ``["회사명 : X", "1. 일반사항", "(1) 개요", "본문"]``.
    제목 줄 뒤의 줄들은 하위 번호 줄(``(1)``, ``2.1``, ``가.``, ``①``, ≤ 60자)을 따로 떼고 나머지 연속 줄은 한 문단으로 합친다.
    제목 줄이 없는 문단은 **첫 줄**이 하위 번호 줄일 때만 그 줄을 떼고(``(2) 종속기업의 개요<br>당기 …``), 나머지 줄은
    공백으로 이어 한 문단으로 돌려준다. 중간 줄은 건드리지 않는다 (목록·표 설명 오분리 방지).
    """
    lines = [ln for ln in (_clean(x) for x in paragraph.split("\n")) if ln]
    if len(lines) <= 1 or not any(_is_title_line(ln) for ln in lines):
        if not lines:
            return []
        if len(lines) > 1 and _is_sub_line(lines[0]):
            # 제목 없는 문단: 첫 줄이 하위 번호 줄이면 그 줄만 subheading 으로 떼고 나머지는 한 문단 (중간 줄은 건드리지 않음)
            return [lines[0], _clean(" ".join(lines[1:]))]
        return [_clean(" ".join(lines))]
    out: list[str] = []
    buf: list[str] = []
    after_title = False  # 제목 줄 뒤의 줄들에만 하위 번호 줄 분리 규칙을 적용 (제목 없는 블록은 기존 동작 유지)

    def flush() -> None:
        nonlocal buf
        if buf:
            out.append(" ".join(buf))
            buf = []

    for ln in lines:
        if _is_title_line(ln):
            flush()
            out.append(ln)
            after_title = True
        elif after_title and _is_sub_line(ln):
            flush()
            out.append(ln)
        else:
            buf.append(ln)
    flush()
    return out


def _texts_from_inline(pieces: list[str]) -> list[str]:
    """인라인 조각(문자열, <br> 은 "\\n")을 합쳐 빈 줄(2개 이상의 줄바꿈) 기준으로 문단을 나눈다.

    문단 안에서 <br> 하나로 붙은 줄이 주석 제목 조건을 만족하면 :func:`_split_heading_lines` 로 더 쪼갠다
    (실측: ``<P>1. 일반사항<BR/>(1) 지배기업의 개요<BR/>본문…</P>``).
    """
    joined = "".join(pieces)
    out: list[str] = []
    for para in _PARA_SPLIT_RE.split(joined):
        out.extend(t for t in _split_heading_lines(para) if t)
    return out


def _p_block(tag: Tag) -> list[_Block]:
    """<p>/<h*>/<ul> 하나를 (빈 줄로 나뉜) 문단 블록들로. 구조 신호를 함께 담는다."""
    classes = tag.get("class") or []
    is_section = any(str(c).startswith("section") for c in classes)
    structure_title: Optional[str] = None
    maybe_truncated = False
    bm = tag if tag.has_attr("bookmarktext") else tag.find(attrs={"bookmarktext": True})
    if bm is not None:
        attr = _clean(str(bm.get("bookmarktext")))
        inner = _clean(bm.get_text(""))
        # bookmarktext 속성은 20자에서 잘리는 경우가 있다 (실측: "22. 영업부문정보 및 고객과의 계약에서 생").
        # 요소 내부 텍스트가 속성값으로 시작하면서 더 길면 내부 텍스트를 쓴다.
        structure_title = inner if inner.startswith(attr) and len(inner) > len(attr) else attr
        maybe_truncated = len(attr) == BOOKMARK_ATTR_LIMIT and not inner
    elif STRUCTURE_P_CLASS in classes:
        structure_title = _clean(tag.get_text(""))
    texts = _texts_from_inline([tag.get_text("")])
    blocks = [_Block("p", t, tag, None, is_section) for t in texts]
    if structure_title and blocks:
        blocks[0].structure_title = structure_title
    elif structure_title:
        blocks = [_Block("p", structure_title, tag, structure_title, is_section)]
    if blocks and structure_title:
        blocks[0].maybe_truncated = maybe_truncated
    return blocks


def _flatten(el: Tag) -> Iterator[_Block]:
    """요소의 자식을 문서 순서대로 블록으로 낸다. 레이아웃 표는 셀 내용으로 재귀, 표 안 <p> 는 세지 않는다."""
    inline: list[str] = []

    def flush() -> Iterator[_Block]:
        nonlocal inline
        if inline:
            for t in _texts_from_inline(inline):
                yield _Block("p", t)
            inline = []

    for ch in el.children:
        if isinstance(ch, NavigableString):
            inline.append(str(ch))
            continue
        if not isinstance(ch, Tag):
            continue
        if ch.name == "table":
            yield from flush()
            if _is_layout_table(ch):
                for tr in _own_rows(ch):
                    for cell in _own_cells(tr):
                        yield from _flatten(cell)
            else:
                yield _Block("table", tag=ch)
        elif ch.name in BLOCK_TAGS:
            yield from flush()
            yield from _p_block(ch)
        elif ch.find(["table", *BLOCK_TAGS]) is not None:  # div 등 블록을 품은 컨테이너
            yield from flush()
            yield from _flatten(ch)
        else:  # span, a, b, font 등 인라인
            inline.append(ch.get_text(""))
    yield from flush()


def _flatten_html(html: str) -> list[_Block]:
    soup = BeautifulSoup(html, "lxml")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    root = soup.body or soup
    return list(_flatten(root))


# ---------- 제목 후보 ----------


def _title_candidates(blocks: list[_Block]) -> tuple[list[_Candidate], list[tuple[str, str]]]:
    """1단계(구조) + 2단계(텍스트) 후보를 문서 순서대로 모은다. 2단계 후보는 3단계에서 빈 구간에만 쓴다."""
    cands: list[_Candidate] = []
    rejected: list[tuple[str, str]] = []
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if b.kind != "p":
            i += 1
            continue
        if b.is_section:
            rejected.append((b.text[:40], "절 제목(section 클래스)"))
            i += 1
            continue
        if b.structure_title is not None:
            h = parse_heading(b.structure_title)
            if h:
                cands.append(_Candidate(i, h[0], h[2], "structure", 1, b.maybe_truncated, branch=h[1]))
            else:
                rejected.append((b.structure_title[:40], "구조 신호이나 'N. 제목' 형식 아님"))
            i += 1
            continue
        text = b.text
        span = 1
        nm = _NUMBER_ONLY_RE.match(text)
        if nm and i + 1 < len(blocks) and blocks[i + 1].kind == "p":
            text = f"{text.strip()} {blocks[i + 1].text}"
            span = 2
        h = parse_heading(text)
        if h:
            title = h[2]
            if len(text) > TITLE_MAX_LEN:
                rejected.append((text[:40], f"길이 {len(text)} > {TITLE_MAX_LEN}"))
            elif title.endswith(TITLE_BAD_ENDINGS):
                rejected.append((text[:40], f"문장 종결({title[-1]!r})로 끝남"))
            else:
                cands.append(_Candidate(i, h[0], title, "text", span, branch=h[1]))
                i += span
                continue
        i += 1
    return cands, rejected


def _validate_sequence(
    cands: list[_Candidate], warnings: list[str], rejected: list[tuple[str, str]], start: int = 0
) -> list[_Candidate]:
    """3단계 순차성 검증. 가지 번호는 ``13 → 14-1 → 14-2 → 15`` 를 허용한다.

    - 새 번호는 직전 +1 (가지 없음 또는 ``-1`` 로 시작). 하나 건너뛰면 누락 경고 후 수용.
    - 같은 번호는 가지가 +1 로 이어져야 한다. ``14.`` 와 ``14-1.`` 이 함께 있으면 14 를 상위로 두고 경고.
    - ``start`` 는 직전 번호의 초깃값(기본 0 → 첫 주석은 1). 자식 노드 하나만 파싱할 때는 그 번호-1 을 준다.
    """
    has_structure = any(c.source == "structure" for c in cands)
    accepted: list[_Candidate] = []
    prev_n, prev_b = start, None
    for c in cands:
        lab = _label(c.number, c.branch)
        text_label = f"{lab}. {c.title}"[:40]
        ok = False
        if c.number == prev_n + 1 and c.branch in (None, 1):
            ok = True  # 새 번호 (가지 없음 또는 -1 로 시작)
        elif c.number == prev_n and c.branch is not None and accepted:
            if prev_b is None and c.branch == 1:
                warnings.append(f"주석 {c.number} 과 {lab} 이 함께 있습니다 — {c.number} 를 상위로 유지")
                ok = True
            elif prev_b is not None and c.branch == prev_b + 1:
                ok = True  # 같은 번호의 다음 가지 (14-1 → 14-2)
        elif c.number == prev_n + 2 and c.branch in (None, 1) and (c.source == "structure" or not has_structure):
            warnings.append(f"주석 {prev_n + 1} 누락 (검출 순서상 {_label(prev_n, prev_b)} → {lab})")
            ok = True
        if ok:
            accepted.append(c)
            prev_n, prev_b = c.number, c.branch
        elif (c.number, c.branch or 0) <= (prev_n, prev_b or 0):
            rejected.append((text_label, f"번호 {lab} ≤ 직전 {_label(prev_n, prev_b)} (중복/역행)"))
        else:
            rejected.append((text_label, f"번호 {lab} 가 직전 {_label(prev_n, prev_b)} 와 연속되지 않음"))
    return accepted


# ---------- 블록 변환 ----------


def _convert_table(
    tag: Tag, acc: _NoteAcc, table_no: int, warnings: list[str]
) -> NoteBlock:
    """데이터 표 → NoteBlock(table). 직전 문단이 단위 문구면 흡수(단위만 있는 문단은 삭제), 하위 제목이면 caption.

    단위가 문단에 없으면 헤더 첫 셀(``(단위: 천원)``)에서 찾는다.
    """
    unit: Optional[str] = None
    caption: Optional[str] = None
    if acc.blocks:
        last = acc.blocks[-1]
        if last.kind in ("paragraph", "subheading") and last.text:
            u = extract_unit(last.text)
            if u:
                unit = u
                rest = _UNIT_PHRASE_RE.sub("", last.text).strip(" :：-")
                if len(rest) <= 5:
                    acc.blocks.pop()
            elif last.kind == "subheading":
                caption = last.text
    grid, depth = html_table_to_grid(tag, return_depth=True, warnings=warnings, table_no=table_no)
    header_count = split_header_rows(grid, count_thead_rows(tag))
    if unit is None and grid and header_count > 0 and grid[0]:
        # 감사보고서는 단위 문구가 표 헤더 첫 셀에 들어간다: <th>(단위: 천원)</th>. 헤더 텍스트는 원문대로 둔다.
        unit = extract_unit(grid[0][0])
    local: list[str] = []
    table = grid_to_table(grid, depth, header_count, caption, unit, local)
    # 주석 표는 텍스트·숫자가 섞인 표가 흔하므로 열별 경고 대신 주석당 1건으로 집계한다 (값은 문자열로 보존됨).
    acc.mixed_tables += 1 if any("숫자 열" in m for m in local) else 0
    warnings.extend(m for m in local if "숫자 열" not in m)
    return NoteBlock(kind="table", table=table)


def _para_block(text: str) -> NoteBlock:
    return NoteBlock(kind="subheading" if is_subheading(text) else "paragraph", text=text)


# ---------- expected_titles ----------


def _norm_title(title: str) -> str:
    """비교용: 괄호와 그 내용, 공백 제거."""
    return _WS_RE.sub("", re.sub(r"[(（][^)）]*[)）]", "", title))


def _apply_expected(notes: list[Note], expected_titles: list[str], warnings: list[str]) -> None:
    expected: dict[tuple[int, Optional[int]], str] = {}
    for t in expected_titles:
        h = parse_heading(t)
        if h:
            expected[(h[0], h[1])] = h[2]
    found = {(n.number, n.branch): n for n in notes}

    def order(k: tuple[int, Optional[int]]) -> tuple[int, int]:
        return (k[0], k[1] or 0)

    for key in sorted(set(expected) - set(found), key=order):
        warnings.append(f"주석 {_label(*key)} 누락(기대 제목: {expected[key]})")
    for key in sorted(set(found) - set(expected), key=order):
        if key[0] != 0:
            warnings.append(f"주석 {_label(*key)} 초과 검출(제목: {found[key].title})")
    for key in sorted(set(found) & set(expected), key=order):
        note = found[key]
        if note.source != "inferred" and _norm_title(note.title) != _norm_title(expected[key]):
            warnings.append(f"주석 {_label(*key)} 제목 불일치: 검출 {note.title!r} / 기대 {expected[key]!r}")
        note.title = expected[key]  # 번호를 뗀 제목 (트리가 정답)
        note.source = "expected"


# ---------- 진입점 ----------


def split_notes(
    html: str,
    scope: Scope,
    warnings: list[str],
    expected_titles: Optional[list[str]] = None,
    debug: Optional[list[tuple[str, str]]] = None,
) -> list[Note]:
    """주석 본문 HTML 을 최상위 주석 번호 단위로 분할한다.

    1. 본문을 블록(문단/표)으로 평탄화한다 (레이아웃 nb 표는 셀 내용으로 풀고, 표 안 <p> 는 세지 않는다).
    2. 제목 후보: 구조 신호(``bookmarktext``, ``p.table-group-xbrl``) → 텍스트 정규식(길이 ≤ 80,
       문장 종결로 끝나지 않음, ``<p>2.</p><p>재고자산</p>`` 분할 제목 결합) → 순차성 검증
       (직전 +1, 하나 건너뛰면 누락 경고). 텍스트 단계는 <br> 한 줄 단위로도 제목을 찾는다.
       첫 주석이 1(expected_titles 가 있으면 그 최소 번호)이 아닐 때: 2..N 이 연속이면 2번 이전 블록을
       ``Note(1, "(제목 미확인)", source="inferred")`` 로 배정하고 경고, 연속 검출이 2개 미만이면
       ``주석00_미분류`` 하나로 반환.
    3. 블록을 현재 주석에 누적: 문단/하위제목/표. 표 직전 단위 문구는 표 unit 으로 흡수, 하위 제목은 caption.
    4. ``expected_titles`` (문서 트리의 자식 제목) 가 있으면 번호 집합·제목을 비교해 경고하고 기대 제목을 쓴다.

    Args:
        html: viewer.do 주석 본문 HTML.
        scope: "연결" | "별도" | "단일".
        warnings: 경고 누적 리스트 (in-place append).
        expected_titles: 트리에서 얻은 주석 제목 목록 (예: ``["1. 일반적 사항 (연결)", ...]``). 없으면 None.
        debug: 주어지면 탈락 후보 ``(텍스트 앞 40자, 사유)`` 를 채운다.

    Returns:
        번호 순서대로의 Note 목록.
    """
    blocks = _flatten_html(html)
    cands, rejected = _title_candidates(blocks)
    # expected_titles 가 주어지면 그 최소 번호부터 시작 (자식 노드 하나만 파싱하는 fallback 에서 18번 등 허용)
    expected_numbers = [h[0] for h in (parse_heading(t) for t in (expected_titles or [])) if h]
    start = min(expected_numbers) - 1 if expected_numbers else 0
    accepted = _validate_sequence(cands, warnings, rejected, start)
    if debug is not None:
        debug.extend(rejected)
    for text, reason in rejected:
        logger.debug("주석 제목 후보 탈락: %r (%s)", text, reason)

    inferred_first = False
    if accepted and accepted[0].number == start + 2 and accepted[0].branch in (None, 1) and len(accepted) >= 2:
        # 1 은 없지만 2..N 이 연속: 첫 제목 이전 블록(기간 표·회사명 포함)을 주석 1 로 배정한다
        inferred_first = True
        first_no = start + 1
        warnings[:] = [w for w in warnings if not w.startswith(f"주석 {first_no} 누락 (검출 순서상")]
        warnings.append(f"주석 {first_no} 제목을 찾지 못해 {first_no + 1}번 이전 블록을 주석 {first_no}로 배정함 — 시트 주석{first_no:02d} 확인 필요")
        accs = [_NoteAcc(first_no, "(제목 미확인)", "inferred")]
    elif not accepted or accepted[0].number != start + 1:
        if accepted:
            warnings.append(f"첫 주석 번호가 {start + 1} 이 아님({accepted[0].number}) → 주석00_미분류 하나로 반환")
        else:
            warnings.append("주석 제목을 하나도 찾지 못함 → 주석00_미분류 하나로 반환")
        accepted = []
        accs = [_NoteAcc(0, "미분류", "text")]
    else:
        accs = [_NoteAcc(0, "미분류", "text")]  # 첫 제목 이전 블록 보관용 (뒤에서 첫 주석에 합침)

    for c in accepted:
        if c.maybe_truncated:
            warnings.append(f"주석 {_label(c.number, c.branch)} 제목이 잘렸을 수 있음 (bookmarktext 20자, 내부 텍스트 없음): {c.title!r}")
    by_index = {c.index: c for c in accepted}
    skip_until = -1
    table_no = 0
    for i, b in enumerate(blocks):
        if i <= skip_until:
            continue
        c = by_index.get(i)
        if c is not None:
            accs.append(_NoteAcc(c.number, c.title, c.source, branch=c.branch))
            skip_until = i + c.span - 1
            continue
        acc = accs[-1]
        if b.kind == "table":
            table_no += 1
            acc.blocks.append(_convert_table(b.tag, acc, table_no, warnings))
        elif b.text and not b.is_section:
            acc.blocks.append(_para_block(b.text))

    if accepted and not inferred_first:
        preamble, accs = accs[0], accs[1:]
        accs[0].blocks = preamble.blocks + accs[0].blocks
        accs[0].mixed_tables += preamble.mixed_tables

    mixed = [(_label(a.number, a.branch), a.mixed_tables) for a in accs if a.mixed_tables]
    if mixed:
        warnings.append(
            f"주석 표 {sum(m for _, m in mixed)}개에서 숫자 열에 문자열 값이 섞여 원문 그대로 남김 "
            f"(주석 {', '.join(n for n, _ in mixed)})"
        )
    notes = [
        Note(number=a.number, title=a.title, scope=scope, blocks=a.blocks, source=a.source, branch=a.branch)
        for a in accs
    ]
    if expected_titles and accepted:
        _apply_expected(notes, expected_titles, warnings)
    return notes
