"""입력 → fetch → tree → statements/notes → excel 을 잇는 오케스트레이션.

app.py 는 이 모듈의 :func:`convert_report` 만 호출한다 (UI 에 파싱 로직을 두지 않기 위함).
네트워크 호출은 :mod:`dart.fetcher` 의 함수를 통해서만 한다.

meta 위치 (실측): main.do 의 ``<title>회사명/보고서명/접수일</title>`` (예: ``삼성전자/사업보고서/2026.03.10``).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Callable, Optional

from dart.excel import build_workbook
from dart.fetcher import MAIN_URL, DartBlockedError, build_session, fetch_html
from dart.models import DocNode, Note, ParsedReport, Scope
from dart.notes import parse_heading, split_notes
from dart.statements import extract_statements
from dart.tree import parse_doc_tree, parse_report_input, select_target_nodes

logger = logging.getLogger(__name__)

# (진행률 0.0~1.0, 상태 메시지)
ProgressCallback = Callable[[float, str], None]

USER_MSG_BLOCKED = "DART가 요청을 거부했습니다. 잠시 후 다시 시도하세요"

_TITLE_RE = re.compile(r"<title>\s*(.*?)\s*</title>", re.I | re.S)
# 본문선택 <option value="rcpNo=20260310002820&amp;dcmNo=11104486" title="…"> 2026.03.10&nbsp; 감사보고서 </option>
_OPTION_RE = re.compile(r'<option\s+value="rcpNo=(\d{14})[^"]*"([^>]*)>(.*?)</option>', re.I | re.S)
_DATE_RE = re.compile(r"(\d{4})\s*[.년]\s*(\d{1,2})\s*[.월]\s*(\d{1,2})")
_BASE_DATE_RE = re.compile(r"[(（]\s*(\d{4}\.\d{1,2})\s*[)）]")

FS_KEYS: tuple[str, ...] = ("연결_재무제표", "별도_재무제표", "재무제표")
NOTE_KEYS: tuple[str, ...] = ("연결_주석", "별도_주석", "주석")


class ConversionError(RuntimeError):
    """사용자에게 보여줄 메시지를 담은 변환 실패 예외."""


def _scope_of(key: str) -> Scope:
    if key.startswith("연결"):
        return "연결"
    if key.startswith("별도"):
        return "별도"
    return "단일"


def extract_meta(main_html: str, rcp_no: str) -> dict:
    """main.do HTML 에서 회사명·보고서명·접수일·기준일을 뽑는다. 못 찾은 값은 넣지 않는다.

    ``<title>회사명/보고서명/접수일</title>`` 을 파싱한다. 기준일은 보고서명 안의 ``(2025.12)`` 형태가
    있을 때만 넣는다. ``rcp_no``, ``source_url``, ``parsed_at``(ISO) 은 항상 넣는다.

    Args:
        main_html: main.do 응답 HTML.
        rcp_no: 접수번호.

    Returns:
        meta dict (키: rcp_no, company, report_name, rcp_date, base_date, source_url, parsed_at).
    """
    meta: dict = {
        "rcp_no": rcp_no,
        "source_url": f"{MAIN_URL}?rcpNo={rcp_no}",
        "parsed_at": datetime.now().isoformat(timespec="seconds"),
    }
    m = _TITLE_RE.search(main_html or "")
    if m:
        parts = [p.strip() for p in re.sub(r"\s+", " ", m.group(1)).split("/")]
        if len(parts) >= 1 and parts[0]:
            meta["company"] = parts[0]
        if len(parts) >= 2 and parts[1]:
            meta["report_name"] = parts[1]
            bd = _BASE_DATE_RE.search(parts[1])
            if bd:
                meta["base_date"] = bd.group(1)
        if len(parts) >= 3 and parts[2]:
            meta["rcp_date"] = parts[2]
    related = related_reports(main_html or "", rcp_no)
    if related:
        meta["related_reports"] = related
    return meta


def related_reports(main_html: str, rcp_no: str) -> list[dict]:
    """본문선택 <option> 목록에서 현재 rcpNo 가 아닌 다른 공시(정정본 등)를 뽑는다.

    Args:
        main_html: main.do HTML.
        rcp_no: 현재 접수번호.

    Returns:
        ``[{"rcp_no", "title", "date", "is_correction"}]`` (중복 제거, 문서 순서).
    """
    out: list[dict] = []
    seen: set[str] = set()
    for m in _OPTION_RE.finditer(main_html):
        other, attrs, text = m.group(1), m.group(2), re.sub(r"\s+|&nbsp;", " ", m.group(3)).strip()
        if other == rcp_no or other in seen:
            continue
        seen.add(other)
        tm = re.search(r'title="([^"]*)"', attrs)
        title = tm.group(1).strip() if tm else re.sub(r"^\d{4}\.\d{2}\.\d{2}\s*", "", text)
        dm = re.search(r"\d{4}\.\d{2}\.\d{2}", text)
        out.append({"rcp_no": other, "title": title, "date": dm.group(0) if dm else "", "is_correction": "정정" in (title + text)})
    return out


def base_date_from_statements(statements: list) -> Optional[str]:
    """재무상태표(연결 → 별도 → 단일 순) period_text 의 첫 날짜를 ``YYYY.MM.DD`` 로 돌려준다."""
    order = {"연결": 0, "별도": 1, "단일": 2}
    for stmt in sorted((s for s in statements if s.kind == "재무상태표"), key=lambda s: order.get(s.scope, 9)):
        m = _DATE_RE.search(stmt.period_text or "")
        if m:
            return f"{m.group(1)}.{int(m.group(2)):02d}.{int(m.group(3)):02d}"
    return None


_TAG_STRIP_RE = re.compile(r"<[^>]+>")
EMPTY_NODE_TEXT_LEN = 300


def _is_empty_node(html: str) -> bool:
    """빈 껍데기 노드 판정: ``<table>`` 이 없고 태그 제거 후 비공백 텍스트가 300자 미만.

    연결재무제표를 작성하지 않는 회사의 정기보고서에서 "2. 연결재무제표" 노드가
    수백 바이트 껍데기로 오는 실측 케이스(20260319000808) 대응. 정상 케이스이므로 경고가 아니다.
    """
    if "<table" in html.lower():
        return False
    text = _TAG_STRIP_RE.sub(" ", html)
    return len(re.sub(r"\s+", "", text)) < EMPTY_NODE_TEXT_LEN


def _notes_mismatch(notes: list[Note], expected_titles: list[str]) -> int:
    """검출 (번호, 가지) 집합과 기대 집합의 대칭 차집합 크기."""
    expected = {(h[0], h[1]) for h in (parse_heading(t) for t in expected_titles) if h}
    found = {(n.number, n.branch) for n in notes}
    return len(expected ^ found)


def _fetch_children_notes(
    session, node: DocNode, scope: Scope, warnings: list[str]
) -> list[Note]:
    """주석 자식 노드를 개별 수신해 주석 하나씩으로 파싱하고 번호순으로 합친다."""
    result: list[Note] = []
    for child in node.children:
        heading = parse_heading(child.title)
        local: list[str] = []
        try:
            html = fetch_html(session, child.url)
            notes = split_notes(html, scope, local, [child.title])
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"[{scope} 주석 자식 {child.title!r}] 수신/파싱 실패: {type(exc).__name__}: {str(exc)[:200]}")
            continue
        if heading and notes:
            chosen = next((n for n in notes if (n.number, n.branch) == (heading[0], heading[1])), notes[0])
            chosen.number, chosen.branch, chosen.title, chosen.source = heading[0], heading[1], heading[2], "expected"
            result.append(chosen)
        elif notes:
            result.extend(notes)
        warnings.extend(w for w in local if "첫 주석 번호" not in w and "초과 검출" not in w and "누락" not in w)
    return sorted(result, key=lambda n: (n.number, n.branch or 0))


def convert_report(
    user_input: str,
    progress: Optional[ProgressCallback] = None,
    split_note_sheets: bool = True,
) -> tuple[ParsedReport, bytes]:
    """DART 입력(URL 또는 rcpNo)을 받아 파싱 결과와 xlsx 바이트를 돌려준다.

    단계와 진행률: rcpNo 파싱(0.05) → main.do 수신·트리(0.15) → 대상 노드·meta(0.20) →
    재무제표 수신·파싱(0.20~0.50, 스코프당 균등) → 주석 수신·파싱(0.50~0.85) → 워크북(0.85~1.0).
    스코프 하나의 수신·파싱 실패는 그 부분만 비우고 ``warnings`` 에 원인을 남긴 뒤 계속한다.
    주석은 트리 자식 제목을 expected_titles 로 넘기고, 누락·초과가 있으면 자식 노드를 개별 수신한다.

    Args:
        user_input: URL 또는 14자리 rcpNo.
        progress: 진행률 콜백. None 이면 호출하지 않는다.
        split_note_sheets: False 면 주석번호별 시트 없이 ``주석_전체`` 만 만든다.

    Returns:
        ``(report, xlsx_bytes)``.

    Raises:
        ConversionError: 입력 오류, DART 차단, 트리 없음, 재무제표·주석 모두 없음.
    """

    def report_progress(value: float, message: str) -> None:
        if progress is not None:
            progress(value, message)

    warnings: list[str] = []
    try:
        rcp_no = parse_report_input(user_input)
    except ValueError as exc:
        raise ConversionError(str(exc)) from exc
    report_progress(0.05, f"접수번호 {rcp_no} 확인")

    session = build_session()
    try:
        main_html = fetch_html(session, f"{MAIN_URL}?rcpNo={rcp_no}")
    except DartBlockedError as exc:
        raise ConversionError(USER_MSG_BLOCKED) from exc
    except Exception as exc:  # noqa: BLE001
        raise ConversionError(f"main.do 수신 실패: {type(exc).__name__}: {str(exc)[:200]}") from exc
    nodes = parse_doc_tree(main_html, rcp_no)
    if not nodes:
        raise ConversionError("문서 트리를 찾을 수 없습니다. rcpNo 가 맞는지, 로그인이 필요한 문서는 아닌지 확인하세요.")
    report_progress(0.15, f"문서 트리 {len(nodes)}개 노드 확인")

    selected = select_target_nodes(nodes, warnings)
    meta = extract_meta(main_html, rcp_no)
    for rel in meta.get("related_reports", []):
        if rel["is_correction"]:
            warnings.append(f"이 공시에 정정본이 있습니다: {rel['date']} {rel['title']} (rcpNo={rel['rcp_no']})")
    report_progress(0.20, "재무제표·주석 노드 선택 완료")

    statements = []
    fs_keys = [k for k in FS_KEYS if k in selected]
    for i, key in enumerate(fs_keys):
        scope = _scope_of(key)
        report_progress(0.20 + 0.30 * i / max(len(fs_keys), 1), f"재무제표({scope}) 수신·파싱 중…")
        try:
            html = fetch_html(session, selected[key].url)
            if _is_empty_node(html):
                logger.info("[%s] 빈 노드(연결 미작성 등) — 이 스코프의 재무제표 없음", key)
                continue
            statements += extract_statements(html, scope, warnings)
        except DartBlockedError as exc:
            warnings.append(f"[{key}] {USER_MSG_BLOCKED}: {str(exc)[:200]}")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"[{key}] 수신/파싱 실패: {type(exc).__name__}: {str(exc)[:200]}")
    if not fs_keys:
        warnings.append("문서 트리에서 재무제표 노드를 찾지 못했습니다.")

    notes: list[Note] = []
    note_keys = [k for k in NOTE_KEYS if k in selected]
    for i, key in enumerate(note_keys):
        scope = _scope_of(key)
        node = selected[key]
        report_progress(0.50 + 0.35 * i / max(len(note_keys), 1), f"주석({scope}) 수신·파싱 중…")
        expected = [c.title for c in node.children] or None
        try:
            html = fetch_html(session, node.url)
            if _is_empty_node(html):
                logger.info("[%s] 빈 노드(연결 미작성 등) — 이 스코프의 주석 없음", key)
                continue
            found = split_notes(html, scope, warnings, expected)
            if expected and _notes_mismatch(found, expected) >= 1:
                warnings.append(f"주석 분할 불일치로 자식 노드 {len(node.children)}개 개별 수신 ({scope})")
                report_progress(0.50 + 0.35 * (i + 0.5) / max(len(note_keys), 1), f"주석({scope}) 자식 노드 개별 수신 중…")
                found = _fetch_children_notes(session, node, scope, warnings)
            notes += found
        except DartBlockedError as exc:
            warnings.append(f"[{key}] {USER_MSG_BLOCKED}: {str(exc)[:200]}")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"[{key}] 수신/파싱 실패: {type(exc).__name__}: {str(exc)[:200]}")
    if not note_keys:
        warnings.append("문서 트리에서 주석 노드를 찾지 못했습니다.")

    base_date = base_date_from_statements(statements)
    if base_date and "base_date" not in meta:
        meta["base_date"] = base_date

    if not statements and not notes:
        raise ConversionError("재무제표와 주석을 하나도 추출하지 못했습니다. " + " / ".join(warnings[-3:]))

    report = ParsedReport(meta=meta, statements=statements, notes=notes, warnings=warnings)
    report_progress(0.85, "Excel 워크북 생성 중…")
    data = build_workbook(report, split_note_sheets=split_note_sheets)
    report_progress(1.0, "완료")
    return report, data
