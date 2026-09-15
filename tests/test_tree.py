"""dart.tree 테스트 (tests/fixtures/main_do_*.html 기반, 네트워크 없음)."""

from __future__ import annotations

import pytest

from dart.models import DocNode
from dart.tree import (
    build_viewer_url,
    describe_tree,
    normalize_title,
    parse_doc_tree,
    parse_report_input,
    select_target_nodes,
)

ANNUAL_RCP = "20260310002820"
AUDIT_RCP = "20260414001300"


# ---------- parse_report_input ----------


@pytest.mark.parametrize(
    "user_input",
    [
        "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260310002820",
        "https://dart.fss.or.kr/report/viewer.do?rcpNo=20260310002820&dcmNo=11104488&eleId=19&offset=1&length=2&dtd=dart4.xsd",
        "20260310002820",
        "   '20260310002820'  \n",
        '"https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260310002820&dcmNo=11104488"',
    ],
)
def test_parse_report_input_ok(user_input: str) -> None:
    assert parse_report_input(user_input) == "20260310002820"


@pytest.mark.parametrize("bad", ["", "abc", "1234", "https://dart.fss.or.kr/dsaf001/main.do", "2026031000282"])
def test_parse_report_input_invalid(bad: str) -> None:
    with pytest.raises(ValueError, match="rcpNo를 찾을 수 없습니다"):
        parse_report_input(bad)


# ---------- normalize_title / build_viewer_url ----------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("재 무 상 태 표", "재무상태표"),
        ("4. 재무제표", "재무제표"),
        ("III. 재무에 관한 사항", "재무에관한사항"),
        ("2-1. 연결 재무상태표", "연결재무상태표"),
        ("Ⅲ. 재무에 관한 사항", "재무에관한사항"),
        ("3. 연결재무제표 주석", "연결재무제표주석"),
        ("(첨부)연 결 재 무 제 표", "(첨부)연결재무제표"),
        ("　주석 ", "주석"),
    ],
)
def test_normalize_title(raw: str, expected: str) -> None:
    assert normalize_title(raw) == expected


def test_build_viewer_url() -> None:
    node = DocNode("t", "20260310002820", "11104488", "19", "100", "200", "dart4.xsd", "")
    url = build_viewer_url(node)
    assert url.startswith("https://dart.fss.or.kr/report/viewer.do?")
    for part in ("rcpNo=20260310002820", "dcmNo=11104488", "eleId=19", "offset=100", "length=200", "dtd=dart4.xsd"):
        assert part in url


# ---------- parse_doc_tree ----------


def test_parse_doc_tree_annual(load_fixture) -> None:
    nodes = parse_doc_tree(load_fixture("main_do_annual.html"), ANNUAL_RCP)
    assert len(nodes) > 10
    assert nodes[0].depth == 0 and nodes[0].parent_title is None
    assert all(n.rcp_no == ANNUAL_RCP for n in nodes)
    assert all(n.dcm_no and n.ele_id and n.offset and n.length and n.dtd for n in nodes)
    for n in nodes:
        for part in (f"dcmNo={n.dcm_no}", f"eleId={n.ele_id}", f"offset={n.offset}", f"length={n.length}", f"dtd={n.dtd}"):
            assert part in n.url
    # depth/parent 관계
    by_title = {normalize_title(n.title): n for n in nodes}
    fs = by_title["연결재무제표"]
    assert fs.depth == 1 and normalize_title(fs.parent_title or "") == "재무에관한사항"
    child = next(n for n in nodes if n.parent_title == fs.title)
    assert child.depth == 2
    # 최상위 노드의 부모는 None
    assert all(n.parent_title is None for n in nodes if n.depth == 0)


def test_parse_doc_tree_audit(load_fixture) -> None:
    nodes = parse_doc_tree(load_fixture("main_do_audit.html"), AUDIT_RCP)
    assert len(nodes) >= 5
    titles = [normalize_title(n.title) for n in nodes]
    assert "(첨부)연결재무제표" in titles
    note = next(n for n in nodes if normalize_title(n.title) == "주석")
    assert note.depth == 1 and normalize_title(note.parent_title or "") == "(첨부)연결재무제표"


def test_parse_doc_tree_empty() -> None:
    assert parse_doc_tree("<html><body>no tree</body></html>", ANNUAL_RCP) == []


# ---------- select_target_nodes ----------


def test_select_target_nodes_annual(load_fixture) -> None:
    nodes = parse_doc_tree(load_fixture("main_do_annual.html"), ANNUAL_RCP)
    warnings: list[str] = []
    sel = select_target_nodes(nodes, warnings)
    assert set(sel) == {"연결_재무제표", "연결_주석", "별도_재무제표", "별도_주석"}
    assert normalize_title(sel["연결_재무제표"].title) == "연결재무제표"
    assert normalize_title(sel["연결_주석"].title) == "연결재무제표주석"
    assert normalize_title(sel["별도_재무제표"].title) == "재무제표"
    assert normalize_title(sel["별도_주석"].title) == "재무제표주석"
    assert warnings == []


def test_select_target_nodes_audit_consolidated(load_fixture) -> None:
    """20260414001300 은 연결감사보고서: 연결_* 만 채우고 별도/단일 키는 없다."""
    nodes = parse_doc_tree(load_fixture("main_do_audit.html"), AUDIT_RCP)
    sel = select_target_nodes(nodes)
    assert set(sel) == {"연결_재무제표", "연결_주석"}
    assert sel["연결_재무제표"].ele_id == "3"
    assert sel["연결_주석"].ele_id == "4"


def _node(title: str, ele_id: str, depth: int = 0, parent: str | None = None) -> DocNode:
    return DocNode(title, AUDIT_RCP, "1", ele_id, "0", "1", "dart4.xsd", "", depth, parent)


def test_select_target_nodes_audit_separate_synthetic() -> None:
    """연결 없는 감사보고서: 재무제표 + (제목이 그냥 '주석') → 키 '재무제표', '주석'."""
    nodes = [
        _node("감 사 보 고 서", "1"),
        _node("독립된 감사인의 감사보고서", "2"),
        _node("(첨부)재 무 제 표", "3"),
        _node("주석", "4", 1, "(첨부)재 무 제 표"),
        _node("외부감사 실시내용", "5"),
    ]
    sel = select_target_nodes(nodes)
    assert set(sel) == {"재무제표", "주석"}
    assert sel["재무제표"].ele_id == "3" and sel["주석"].ele_id == "4"


def test_select_target_nodes_excludes_and_prefers_deepest() -> None:
    nodes = [
        _node("III. 재무에 관한 사항", "17"),
        _node("1. 요약재무정보", "18", 1),
        _node("4. 재무제표", "60", 1),
        _node("4. 재무제표", "61", 2, "4. 재무제표"),  # 같은 키 후보 2개 → 더 깊은 것
        _node("5. 재무제표 주석", "66", 1),
        _node("8. 기타 재무에 관한 사항", "103", 1),
        _node("재무제표등의 확정", "999", 1),
    ]
    warnings: list[str] = []
    sel = select_target_nodes(nodes, warnings)
    assert set(sel) == {"재무제표", "주석"}
    assert sel["재무제표"].ele_id == "61"
    assert len(warnings) == 1 and "재무제표" in warnings[0]


def test_describe_tree(load_fixture) -> None:
    nodes = parse_doc_tree(load_fixture("main_do_audit.html"), AUDIT_RCP)
    out = describe_tree(nodes)
    assert "  주석  [eleId=4" in out
    assert out.count("\n") == len(nodes) - 1



# ---------- children ----------


def test_children_preserved(load_fixture) -> None:
    nodes = parse_doc_tree(load_fixture("main_do_annual.html"), ANNUAL_RCP)
    fs = next(n for n in nodes if normalize_title(n.title) == "연결재무제표")
    assert [normalize_title(c.title) for c in fs.children] == [
        "연결재무상태표", "연결손익계산서", "연결포괄손익계산서", "연결자본변동표", "연결현금흐름표",
    ]
    assert all(c.parent_title == fs.title for c in fs.children)
    notes = next(n for n in nodes if normalize_title(n.title) == "연결재무제표주석")
    assert len(notes.children) == 34
    assert all(n.children == [] for n in nodes if n.depth == 2)


def test_children_audit(load_fixture) -> None:
    nodes = parse_doc_tree(load_fixture("main_do_audit.html"), AUDIT_RCP)
    fs = next(n for n in nodes if normalize_title(n.title) == "(첨부)연결재무제표")
    assert [c.title for c in fs.children] == ["주석"]
    sel = select_target_nodes(nodes)
    assert sel["연결_재무제표"].length == "460049"  # 서버가 length 를 무시하므로 노드는 원본 그대로
