"""dart.pipeline 테스트: 가짜 fetcher(fixture 8개)로 네트워크 없이 end-to-end + 실제 네트워크(network 마커)."""

from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path

import pytest
from openpyxl import load_workbook

from dart import pipeline
from dart.fetcher import DartBlockedError
from dart.pipeline import ConversionError, _fetch_children_notes, base_date_from_statements, convert_report, extract_meta, related_reports

FIX = Path(__file__).parent / "fixtures"
ANNUAL, AUDIT, AUDIT_SEP, AUDIT_SFOOD = "20260310002820", "20260414001300", "20260911000443", "20260409001232"
NOCONSOL = "20260319000808"

# eleId → fixture 파일 (viewer.do 는 eleId 만으로 응답을 결정한다)
_ELE_MAP = {
    (ANNUAL, "19"): "fs_annual_consolidated.html",
    (ANNUAL, "25"): "notes_annual_consolidated.html",
    (ANNUAL, "60"): "fs_annual_separate.html",
    (ANNUAL, "66"): "notes_annual_separate.html",
    (AUDIT, "3"): "fs_audit.html",
    (AUDIT, "4"): "notes_audit.html",
    (AUDIT_SEP, "3"): "fs_audit_separate.html",
    (AUDIT_SEP, "8"): "notes_audit_separate.html",
    (AUDIT_SFOOD, "3"): "fs_audit_sfood.html",
    (AUDIT_SFOOD, "4"): "notes_audit_sfood.html",
    (NOCONSOL, "19"): "fs_annual_noconsol_empty.html",  # 연결 미작성 → 빈 껍데기
    (NOCONSOL, "20"): "notes_annual_noconsol_empty.html",
    (NOCONSOL, "21"): "fs_annual_separate.html",  # 실제 노드(ele_21)는 IP 차단으로 미확보 → 삼성 별도로 대체
    (NOCONSOL, "26"): "notes_annual_noconsol.html",
}
_MAIN_MAP = {
    ANNUAL: "main_do_annual.html", AUDIT: "main_do_audit.html",
    AUDIT_SEP: "main_do_audit_separate.html", AUDIT_SFOOD: "main_do_audit_sfood.html",
    NOCONSOL: "main_do_annual_noconsol.html",
}


def _params(url: str) -> dict[str, str]:
    return dict(re.findall(r"[?&]([^=&]+)=([^&]*)", url))


def _make_fake_fetch(calls: list[str], fail_ele: set[str] = frozenset(), blocked: bool = False):
    def fake_fetch(session, url: str) -> str:
        calls.append(url)
        p = _params(url)
        rcp = p["rcpNo"]
        if "main.do" in url:
            if blocked:
                raise DartBlockedError(url, "접근이 거부되었습니다")
            return (FIX / _MAIN_MAP[rcp]).read_text(encoding="utf-8")
        ele = p["eleId"]
        if ele in fail_ele:
            raise RuntimeError(f"fake failure eleId={ele}")
        name = _ELE_MAP.get((rcp, ele))
        if name:
            return (FIX / name).read_text(encoding="utf-8")
        # 주석 자식 노드: 제목 + 본문 한 문단짜리 합성 HTML
        title = p.get("_title", f"{ele}. 자식")
        return f'<html><body><p class="table-group-xbrl">{title}</p><p>자식 본문 {ele}</p></body></html>'

    return fake_fetch


@pytest.fixture
def fake_fetch(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    monkeypatch.setattr(pipeline, "fetch_html", _make_fake_fetch(calls))
    return calls


# ---------- extract_meta ----------


def test_extract_meta_annual(load_fixture) -> None:
    meta = extract_meta(load_fixture("main_do_annual.html"), ANNUAL)
    assert meta["company"] == "삼성전자" and meta["report_name"] == "사업보고서" and meta["rcp_date"] == "2026.03.10"
    assert meta["rcp_no"] == ANNUAL and meta["source_url"].endswith(ANNUAL) and "T" in meta["parsed_at"]
    assert "base_date" not in meta


def test_extract_meta_audit(load_fixture) -> None:
    meta = extract_meta(load_fixture("main_do_audit.html"), AUDIT)
    assert meta["company"] == "리벨리온" and meta["report_name"] == "연결감사보고서" and meta["rcp_date"] == "2026.04.14"


def test_extract_meta_audit_separate(load_fixture) -> None:
    meta = extract_meta(load_fixture("main_do_audit_separate.html"), AUDIT_SEP)
    assert meta["company"] == "나이키코리아" and meta["report_name"] == "감사보고서" and meta["rcp_date"] == "2026.09.11"
    assert "related_reports" not in meta  # 본문선택에 다른 rcpNo 없음


def test_related_reports_correction() -> None:
    html = (
        '<select><option value="rcpNo=20260310002820" selected title="사업보고서"> 2026.03.10&nbsp; 사업보고서 </option>'
        '<option value="rcpNo=20260401000111" title="[기재정정]사업보고서"> 2026.04.01&nbsp; [기재정정]사업보고서 </option>'
        '<option value="rcpNo=20260310002820&amp;dcmNo=11104486"> 2026.03.10&nbsp; 감사보고서 </option>'
        '<option value="rcpNo=20260401000111&amp;dcmNo=1"> 중복 </option></select>'
    )
    rel = related_reports(html, "20260310002820")
    assert rel == [{"rcp_no": "20260401000111", "title": "[기재정정]사업보고서", "date": "2026.04.01", "is_correction": True}]
    meta = extract_meta("<title>회사/사업보고서/2026.03.10</title>" + html, "20260310002820")
    assert meta["related_reports"] == rel


def test_correction_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real = _make_fake_fetch(calls)

    def fetch_with_correction(session, url: str) -> str:
        html = real(session, url)
        if "main.do" in url:
            html = html.replace("</select>", '<option value="rcpNo=20260501000999" title="[기재정정]연결감사보고서"> 2026.05.01&nbsp; [기재정정]연결감사보고서 </option></select>', 1)
        return html

    monkeypatch.setattr(pipeline, "fetch_html", fetch_with_correction)
    report, _ = convert_report(AUDIT)
    assert "이 공시에 정정본이 있습니다: 2026.05.01 [기재정정]연결감사보고서 (rcpNo=20260501000999)" in report.warnings


def test_base_date_from_statements(load_fixture) -> None:
    from dart.statements import extract_statements

    stmts = extract_statements(load_fixture("fs_annual_separate.html"), "별도", []) + extract_statements(load_fixture("fs_audit.html"), "연결", [])
    assert base_date_from_statements(stmts) == "2025.12.31"  # 연결(감사보고서 "2025년 12월 31일 현재") 우선
    assert base_date_from_statements([]) is None


def test_extract_meta_missing_title() -> None:
    meta = extract_meta("<html></html>", ANNUAL)
    assert set(meta) == {"rcp_no", "source_url", "parsed_at"}


def test_extract_meta_base_date() -> None:
    meta = extract_meta("<title>회사/사업보고서 (2025.12)/2026.03.10</title>", ANNUAL)
    assert meta["base_date"] == "2025.12"


# ---------- end-to-end (fake fetcher) ----------


def _run(user_input: str, **kw):
    values: list[float] = []
    messages: list[str] = []

    def cb(v: float, m: str) -> None:
        values.append(v)
        messages.append(m)

    report, data = convert_report(user_input, cb, **kw)
    return report, data, values, messages


def test_e2e_annual(fake_fetch) -> None:
    report, data, values, messages = _run(f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={ANNUAL}")
    assert values == sorted(values) and values[-1] == 1.0 and values[0] == 0.05
    assert any("재무제표(연결)" in m for m in messages) and any("주석(별도)" in m for m in messages)
    wb = load_workbook(BytesIO(data))
    assert len(wb.sheetnames) == 80
    assert report.meta["company"] == "삼성전자"
    assert len(report.statements) == 10 and len(report.notes) == 66
    assert not any("불일치" in w or "실패" in w for w in report.warnings)
    assert len(fake_fetch) == 5  # main.do + 노드 4개 (fallback 없음)


def test_e2e_audit(fake_fetch) -> None:
    report, data, values, _ = _run(AUDIT)
    assert values[-1] == 1.0
    wb = load_workbook(BytesIO(data))
    assert len(wb.sheetnames) == 37
    assert report.meta["report_name"] == "연결감사보고서"
    assert len(report.statements) == 4 and len(report.notes) == 30
    assert len(fake_fetch) == 3


def test_e2e_audit_separate(fake_fetch) -> None:
    report, data, values, _ = _run(AUDIT_SEP)
    assert values[-1] == 1.0
    names = load_workbook(BytesIO(data)).sheetnames
    assert names[:6] == ["목차", "정보", "재무상태표", "손익계산서", "자본변동표", "현금흐름표"]  # 접두어 없음, 포괄손익 없음
    assert len([n for n in names if n.startswith("주석") and n != "주석_전체"]) == 22 and names[-1] == "주석_전체"
    assert report.meta["company"] == "나이키코리아" and report.meta["base_date"] == "2026.05.31"
    bs = load_workbook(BytesIO(data))["재무상태표"]
    assert [c.value for c in bs[5][:3]] == ["과 목", "주석", "제 16(당) 기"]  # (주석3, 20) 분리 → 주석 열 삽입
    assert {str(m) for m in bs.merged_cells.ranges} == {"C5:D5", "E5:F5"} and bs.freeze_panes == "C6"
    cash = next(r for r in bs.iter_rows(min_row=6) if r[0].value == "현금및현금성자산")
    assert cash[1].value == "3,20" and cash[2].value == 189584809919
    assert all(s.scope == "단일" for s in report.statements) and all(n.scope == "단일" for n in report.notes)
    assert not any("파싱 오류 가능" in w or "대차" in w for w in report.warnings)
    assert len(fake_fetch) == 3


def test_e2e_audit_sfood(fake_fetch) -> None:
    """에쓰푸드 연결감사보고서: 주석 27개 분할, 대차 검증 통과, 시트 2+5+27+1."""
    report, data, values, _ = _run(AUDIT_SFOOD)
    assert values[-1] == 1.0
    names = load_workbook(BytesIO(data)).sheetnames
    assert names[:7] == ["목차", "정보", "재무상태표", "손익계산서", "포괄손익계산서", "자본변동표", "현금흐름표"]
    assert len(names) == 2 + 5 + 27 + 1 and names[7] == "주석01_일반사항" and names[-1] == "주석_전체"
    assert [n.number for n in report.notes] == list(range(1, 28))
    assert report.meta["company"] == "에쓰푸드" and report.meta["base_date"] == "2025.12.31"
    assert not any("대차" in w or "파싱 오류 가능" in w or "미분류" in w or "누락" in w for w in report.warnings)
    assert len(fake_fetch) == 3


def test_e2e_noconsol(fake_fetch) -> None:
    """부산주공(연결 미작성): 빈 연결 노드는 조용히 건너뛰고, 스코프는 별도 하나 → 시트 접두어 없음."""
    report, data, values, _ = _run(NOCONSOL)
    assert values[-1] == 1.0
    scopes = {s.scope for s in report.statements} | {n.scope for n in report.notes}
    assert scopes == {"별도"}
    assert 4 <= len(report.statements) <= 5
    names = load_workbook(BytesIO(data)).sheetnames
    assert names[2] == "재무상태표" and not any(n.startswith(("연결", "별도")) for n in names)  # 접두어 없음
    assert len(report.notes) >= 10
    labels = [f"{n.number}-{n.branch}" if n.branch else str(n.number) for n in report.notes]
    assert "8-1" in labels and "8-2" in labels  # 가지 번호 주석
    assert not any("미분류" in w or "찾지 못했습니다" in w or "F_NO" in w for w in report.warnings)
    assert not any("작성기준" in (s.title or "") for s in report.statements)  # 주석 자식 오선택 없음


def test_e2e_annual_base_date(fake_fetch) -> None:
    report, _, _, _ = _run(ANNUAL)
    assert report.meta["base_date"] == "2025.12.31"
    assert not any("파싱 오류 가능" in w for w in report.warnings)


def test_e2e_no_split_sheets(fake_fetch) -> None:
    _, data, _, _ = _run(AUDIT, split_note_sheets=False)
    names = load_workbook(BytesIO(data)).sheetnames
    assert names == ["목차", "정보", "재무상태표", "포괄손익계산서", "자본변동표", "현금흐름표", "주석_전체"]


def test_bad_input_raises() -> None:
    with pytest.raises(ConversionError, match="rcpNo"):
        convert_report("abc")


def test_blocked_main(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline, "fetch_html", _make_fake_fetch([], blocked=True))
    with pytest.raises(ConversionError, match="DART가 요청을 거부했습니다"):
        convert_report(ANNUAL)


# ---------- fallback: 자식 노드 개별 수신 ----------


def test_notes_fallback_when_expected_mismatch(monkeypatch: pytest.MonkeyPatch, fake_fetch) -> None:
    from dart.models import DocNode
    from dart.tree import select_target_nodes as real_select

    def fake_select(nodes, warnings=None):
        sel = real_select(nodes, warnings)
        node = sel["연결_주석"]
        extra = DocNode("35. 가짜 주석 (연결)", ANNUAL, node.dcm_no, "999", "0", "1", "dart4.xsd", "", 2, node.title)
        extra.url = f"https://dart.fss.or.kr/report/viewer.do?rcpNo={ANNUAL}&dcmNo={node.dcm_no}&eleId=999&_title=35.+가짜+주석+(연결)"
        node.children.append(extra)  # 기대 제목 35개 vs 검출 34개 → 불일치 → fallback
        for c in node.children[:-1]:
            c.url += "&_title=" + c.title.replace(" ", "+")
        return sel

    monkeypatch.setattr(pipeline, "select_target_nodes", fake_select)
    report, data, _, messages = _run(ANNUAL)
    child_calls = [u for u in fake_fetch if "eleId=999" in u or "eleId=26&" in u or "eleId=59&" in u]
    assert len(child_calls) == 3  # 26(1번), 59(34번), 999(가짜 35번) 모두 개별 수신
    assert sum(1 for u in fake_fetch if "_title=" in u) == 35
    assert any("자식 노드 35개 개별 수신" in w for w in report.warnings)
    assert any("자식 노드 개별 수신" in m for m in messages)
    consol = [n for n in report.notes if n.scope == "연결"]
    assert [n.number for n in consol] == list(range(1, 36))
    assert consol[0].title == "일반적 사항 (연결)" and consol[-1].title == "가짜 주석 (연결)"
    assert consol[0].blocks and consol[0].blocks[0].text == "자식 본문 26"
    sep = [n for n in report.notes if n.scope == "별도"]
    assert len(sep) == 32  # 별도는 정상 경로
    assert len(load_workbook(BytesIO(data)).sheetnames) == 2 + 10 + 35 + 32 + 2


# ---------- 부분 실패 ----------


def test_partial_failure_notes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(pipeline, "fetch_html", _make_fake_fetch(calls, fail_ele={"25", "66"}))
    report, data = convert_report(ANNUAL)
    assert len(report.statements) == 10 and report.notes == []
    fails = [w for w in report.warnings if "수신/파싱 실패" in w]
    assert len(fails) == 2 and "RuntimeError: fake failure eleId=25" in fails[0]
    names = load_workbook(BytesIO(data)).sheetnames
    assert len(names) == 12 and "연결주석_전체" not in names


def test_all_failed_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline, "fetch_html", _make_fake_fetch([], fail_ele={"3", "4"}))
    with pytest.raises(ConversionError, match="하나도 추출하지 못했습니다"):
        convert_report(AUDIT)


# ---------- real network ----------


@pytest.mark.network
@pytest.mark.parametrize("rcp_no, sheets", [(ANNUAL, 80), (AUDIT, 37)])
def test_real_network_e2e(rcp_no: str, sheets: int) -> None:
    report, data = convert_report(rcp_no)
    assert len(load_workbook(BytesIO(data)).sheetnames) == sheets
    assert report.meta.get("company")


@pytest.mark.network
def test_real_children_fallback_fetch(load_fixture) -> None:
    """사업보고서 연결 주석 자식 노드 3개(첫·중간·마지막)를 실제 수신해 자식별 split_notes 경로를 검증한다."""
    from dart.fetcher import build_session
    from dart.tree import parse_doc_tree, select_target_nodes

    sel = select_target_nodes(parse_doc_tree(load_fixture("main_do_annual.html"), ANNUAL))
    node = sel["연결_주석"]
    node.children = [node.children[0], node.children[17], node.children[-1]]
    warnings: list[str] = []
    notes = _fetch_children_notes(build_session(), node, "연결", warnings)
    assert [(n.number, n.title, n.source) for n in notes] == [
        (1, "일반적 사항 (연결)", "expected"), (18, "자본금 (연결)", "expected"), (34, "보고기간후사건 (연결)", "expected")
    ]
    assert all(len(n.blocks) >= 1 for n in notes)
    assert notes[0].blocks[0].kind == "subheading" and notes[0].blocks[0].text == "가. 연결회사의 개요"
    assert not any("실패" in w for w in warnings)
