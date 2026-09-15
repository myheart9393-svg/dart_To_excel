"""dart.notes 테스트: 실데이터 3종(트리 자식 제목과 대조) + 합성 fixture(tests/fixtures/notes/)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from dart.excel import note_sheet_name
from dart.models import Note
from dart.notes import is_note_heading, is_subheading, split_notes
from dart.tree import parse_doc_tree, select_target_nodes

NOTES_DIR = Path(__file__).parent / "fixtures" / "notes"


def _load(name: str) -> str:
    return (NOTES_DIR / name).read_text(encoding="utf-8")


def _expected_titles(load_fixture, main_fixture: str, rcp_no: str, key: str) -> list[str]:
    nodes = parse_doc_tree(load_fixture(main_fixture), rcp_no)
    return [c.title for c in select_target_nodes(nodes)[key].children]


def _strip_numbers(titles: list[str]) -> list[str]:
    return [is_note_heading(t)[1] for t in titles]  # type: ignore[index]


def _non_mixed(warnings: list[str]) -> list[str]:
    """주석 표의 숫자·문자열 혼입 집계 경고(호출당 1줄)를 제외한 나머지."""
    return [w for w in warnings if not w.startswith("주석 표 ")]


# ---------- helpers ----------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("1. 일반 사항", (1, "일반 사항")),
        ("21. 우발부채와 약정사항", (21, "우발부채와 약정사항")),
        ("3．재고자산", (3, "재고자산")),
        ("2.1 기준", None),
        ("(1) 담보", None),
        ("가. 개요", None),
        ("", None),
    ],
)
def test_is_note_heading(text: str, expected) -> None:
    assert is_note_heading(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [("2.1 기준", True), ("(1) 담보제공자산", True), ("가. 개요", True), ("① 항목", True), ("2-1 소계", True),
     ("일반 문장입니다.", False), ("(1) " + "가" * 70, False), ("", False)],
)
def test_is_subheading(text: str, expected: bool) -> None:
    assert is_subheading(text) == expected


# ---------- real data ----------


def test_annual_consolidated(load_fixture) -> None:
    expected = _expected_titles(load_fixture, "main_do_annual.html", "20260310002820", "연결_주석")
    assert len(expected) == 34
    warnings: list[str] = []
    debug: list[tuple[str, str]] = []
    t0 = time.perf_counter()
    notes = split_notes(load_fixture("notes_annual_consolidated.html"), "연결", warnings, expected, debug)
    assert time.perf_counter() - t0 < 5.0
    assert [n.number for n in notes] == list(range(1, 35))
    assert all(n.source == "expected" for n in notes)
    assert [n.title for n in notes] == _strip_numbers(expected)
    assert all(len(n.blocks) >= 1 for n in notes)
    assert sum(1 for n in notes for b in n.blocks if b.kind == "table") > 150
    assert _non_mixed(warnings) == []
    assert len(warnings) - len(_non_mixed(warnings)) <= 1
    n1 = notes[0]
    assert n1.blocks[0].kind == "subheading" and n1.blocks[0].text == "가. 연결회사의 개요"
    assert any(b.kind == "table" and b.table.rows for b in n1.blocks)


def test_annual_separate(load_fixture) -> None:
    expected = _expected_titles(load_fixture, "main_do_annual.html", "20260310002820", "별도_주석")
    assert len(expected) == 32
    warnings: list[str] = []
    notes = split_notes(load_fixture("notes_annual_separate.html"), "별도", warnings, expected)
    assert [n.number for n in notes] == list(range(1, 33))
    assert [n.title for n in notes] == _strip_numbers(expected)
    assert all(len(n.blocks) >= 1 for n in notes)
    assert _non_mixed(warnings) == []


def test_audit(load_fixture) -> None:
    warnings: list[str] = []
    debug: list[tuple[str, str]] = []
    notes = split_notes(load_fixture("notes_audit.html"), "연결", warnings, None, debug)
    nums = [n.number for n in notes]
    assert nums == list(range(1, 31)) and len(nums) >= 10
    assert all(n.source == "structure" for n in notes)
    assert notes[0].title == "일반 사항" and notes[-1].title == "특수관계자거래"
    assert notes[21].title == "영업부문정보 및 고객과의 계약에서 생기는 수익"  # bookmarktext 20자 절단 → 내부 텍스트 사용
    assert all(len(n.blocks) >= 1 for n in notes)
    assert _non_mixed(warnings) == []
    # 첫 제목 이전의 기간 문구(nb 표)는 첫 주석 앞에 붙는다
    assert notes[0].blocks[0].kind == "paragraph" and "제 4(당) 기" in (notes[0].blocks[0].text or "")
    assert any(b.kind == "subheading" and b.text == "1.1 종속기업 현황" for b in notes[0].blocks)
    tables = [b.table for n in notes for b in n.blocks if b.kind == "table"]
    assert len(tables) >= 100
    assert any(t.unit for t in tables)


def test_audit_separate(load_fixture) -> None:
    """별도 감사보고서(나이키코리아): 구조 신호 없음 → 텍스트 정규식으로 22개 검출."""
    warnings: list[str] = []
    notes = split_notes(load_fixture("notes_audit_separate.html"), "단일", warnings)
    assert [n.number for n in notes] == list(range(1, 23))
    assert all(n.source == "text" for n in notes)
    assert notes[0].title == "회사의 개요" and notes[-1].title == "현금흐름표"
    assert any(b.kind == "subheading" and b.text == "2.1 재무제표 작성기준" for b in notes[1].blocks)
    assert _non_mixed(warnings) == []


@pytest.mark.parametrize(
    "ele, number, title",
    [("26", 1, "일반적 사항 (연결)"), ("43", 18, "자본금 (연결)"), ("59", 34, "보고기간후사건 (연결)")],
)
def test_child_node_single_note(load_fixture, ele: str, number: int, title: str) -> None:
    """주석 자식 노드 HTML 하나 = 주석 하나. expected_titles 로 시작 번호를 알려주면 그 번호로 검출된다."""
    warnings: list[str] = []
    notes = split_notes(load_fixture(f"note_child_{ele}.html"), "연결", warnings, [f"{number}. {title}"])
    assert [(n.number, n.title, n.source) for n in notes] == [(number, title, "expected")]
    assert len(notes[0].blocks) >= 1
    assert _non_mixed(warnings) == []


def test_audit_sfood(load_fixture) -> None:
    """에쓰푸드 연결감사보고서: 주석 1 제목이 <br> 하나로 본문과 붙어 있음 → 줄 단위 검출."""
    warnings: list[str] = []
    debug: list[tuple[str, str]] = []
    notes = split_notes(load_fixture("notes_audit_sfood.html"), "연결", warnings, None, debug)
    assert [n.number for n in notes] == list(range(1, 28))
    assert notes[0].title == "일반사항" and notes[0].source == "text"
    assert notes[-1].title == "영업활동에서 창출된 현금"
    assert all(n.source == "text" for n in notes)
    # 제목 줄 뒤의 "(1) 지배기업의 개요" 는 주석 1 의 첫 하위 제목, 그 앞의 기간 표는 주석 1 앞에
    kinds = [(b.kind, b.text) for b in notes[0].blocks]
    assert ("paragraph", "제 8 기 2025년 12월 31일 현재") in kinds
    # 제목 줄 뒤의 줄들: 하위 번호 줄은 subheading, 나머지는 paragraph
    body = [(b.kind, (b.text or "")[:12]) for b in notes[0].blocks if b.kind != "table"][3:6]
    assert body == [
        ("subheading", "(1) 지배기업의 개요"), ("paragraph", "에쓰푸드 주식회사(이하"),
        # "(2) 종속기업의 개요<BR/>당기 …" 는 <BR/><BR/> 뒤의 별도 문단(제목 없음)이라 기존 규칙대로 한 블록(짧아서 subheading)
        ("subheading", "(2) 종속기업의 개요"),
    ]
    assert notes[0].blocks[6].kind == "table"
    assert not any("미분류" in w or "누락" in w or "제목을 찾지 못해" in w for w in warnings)
    assert _non_mixed(warnings) == []


def test_regression_other_audits_unchanged(load_fixture) -> None:
    """줄 단위 검출을 넣어도 리벨리온·나이키 결과(주석 수·제목)는 그대로."""
    a = split_notes(load_fixture("notes_audit.html"), "연결", [])
    assert [n.number for n in a] == list(range(1, 31)) and a[0].title == "일반 사항" and a[-1].title == "특수관계자거래"
    assert sum(len(n.blocks) for n in a) == 553
    b = split_notes(load_fixture("notes_audit_separate.html"), "단일", [])
    assert [n.number for n in b] == list(range(1, 23)) and b[0].title == "회사의 개요" and b[-1].title == "현금흐름표"
    c = split_notes(load_fixture("notes_annual_consolidated.html"), "연결", [])
    assert [n.number for n in c] == list(range(1, 35)) and sum(len(n.blocks) for n in c) == 715


# ---------- synthetic ----------


def test_false_positive() -> None:
    warnings: list[str] = []
    debug: list[tuple[str, str]] = []
    notes = split_notes(_load("false_positive.html"), "단일", warnings, None, debug)
    assert [(n.number, n.title) for n in notes] == [(1, "일반 사항"), (2, "회계정책"), (3, "재고자산")]
    assert any("문장 종결" in reason for _, reason in debug)
    assert any(b.kind == "paragraph" and b.text.startswith("3. 이 회사는") for b in notes[1].blocks)
    tbl = next(b for b in notes[2].blocks if b.kind == "table")
    assert tbl.table.unit == "천원"
    assert not any(b.kind == "paragraph" and "단위" in (b.text or "") for b in notes[2].blocks)
    assert warnings == []


def test_split_title() -> None:
    warnings: list[str] = []
    notes = split_notes(_load("split_title.html"), "단일", warnings)
    assert [(n.number, n.title, n.source) for n in notes] == [(1, "일반 사항", "text"), (2, "재고자산", "text"), (3, "매출채권", "text")]
    assert [b.text for b in notes[1].blocks if b.kind == "paragraph"] == ["본문2"]
    assert warnings == []


def test_span_title() -> None:
    notes = split_notes(_load("span_title.html"), "단일", [])
    assert [(n.number, n.title) for n in notes] == [(1, "일반 사항"), (2, "회계정책"), (3, "매출채권")]


def test_number_in_table() -> None:
    warnings: list[str] = []
    notes = split_notes(_load("number_in_table.html"), "단일", warnings)
    assert [n.number for n in notes] == [1, 2, 3]
    tbl = next(b for b in notes[1].blocks if b.kind == "table")
    assert tbl.table.rows[0][0] == "4. 대손충당금" and tbl.table.rows[0][1] == -10.0
    assert warnings == []


def test_skip_number() -> None:
    warnings: list[str] = []
    notes = split_notes(_load("skip_number.html"), "단일", warnings)
    assert [n.number for n in notes] == list(range(1, 13)) + [14, 15]
    assert warnings == ["주석 13 누락 (검출 순서상 12 → 14)"]


def test_no_first() -> None:
    warnings: list[str] = []
    debug: list[tuple[str, str]] = []
    notes = split_notes(_load("no_first.html"), "단일", warnings, None, debug)
    assert len(notes) == 1 and notes[0].number == 0 and notes[0].title == "미분류"
    assert any("주석00_미분류" in w for w in warnings)
    texts = [b.text for b in notes[0].blocks if b.kind != "table"]
    assert "5. 충당부채" in texts and "7. 우발부채" in texts  # 제목이 아닌 문단으로 보존
    assert sum(1 for b in notes[0].blocks if b.kind == "table") == 1
    assert note_sheet_name(notes[0]) == "주석00_미분류"


def test_nb_layout() -> None:
    warnings: list[str] = []
    notes = split_notes(_load("nb_layout.html"), "단일", warnings)
    assert [(n.number, n.source) for n in notes] == [(1, "structure"), (2, "structure")]
    kinds_texts = [(b.kind, b.text) for b in notes[0].blocks]
    assert ("paragraph", "제 4(당) 기 : 2025년 1월 1일부터 2025년 12월 31일까지") in kinds_texts
    assert ("paragraph", "리벨리온 주식회사") in kinds_texts
    assert ("subheading", "1.1 종속기업 현황") in kinds_texts
    assert ("paragraph", "(주1)") in kinds_texts and ("paragraph", "당기 중 신규 설립되었습니다.") in kinds_texts
    assert ("subheading", "가. 개요") in kinds_texts and ("paragraph", "본문 문단입니다.") in kinds_texts
    assert sum(1 for b in notes[0].blocks if b.kind == "table") == 1  # 레이아웃 표 안의 데이터 표
    t2 = next(b for b in notes[1].blocks if b.kind == "table")
    assert t2.table.unit == "원" and [b.kind for b in notes[1].blocks] == ["table"]
    assert warnings == []


def test_expected_titles_mismatch() -> None:
    warnings: list[str] = []
    notes = split_notes(_load("split_title.html"), "단일", warnings, ["1. 일반 사항", "2. 재고자산 (주1)", "4. 차입금"])
    assert [n.title for n in notes] == ["일반 사항", "재고자산 (주1)", "매출채권"]
    assert notes[0].source == "expected" and notes[2].source == "text"
    assert "주석 4 누락(기대 제목: 차입금)" in warnings
    assert "주석 3 초과 검출(제목: 매출채권)" in warnings
    assert not any("불일치" in w for w in warnings)  # (주1) 은 비교에서 제외


def test_heading_after_br() -> None:
    """<p>회사명 : X<br/>1. 일반사항<br/>(1) 개요</p> → 앞 줄은 직전 문단, 제목 줄, 뒤 줄은 새 주석 첫 블록."""
    warnings: list[str] = []
    debug: list[tuple[str, str]] = []
    notes = split_notes(_load("heading_after_br.html"), "단일", warnings, None, debug)
    assert [(n.number, n.title, n.source) for n in notes] == [(1, "일반사항", "text"), (2, "재무제표 작성기준", "text"), (3, "재고자산", "text")]
    n1 = [(b.kind, b.text) for b in notes[0].blocks]
    assert n1 == [
        ("paragraph", "제 8 기 2025년 12월 31일 현재"),
        ("paragraph", "회사명 : X"),  # 제목 줄 앞의 줄 → 직전(선두) 문단
        ("subheading", "(1) 개요"),  # 제목 줄 뒤: 하위 번호 줄은 subheading
        ("paragraph", "지배회사는 2018년에 설립되었습니다."),  # 그 외 줄은 paragraph
    ]
    # "3. 이 회사는 … 있다." 는 문장 종결로 끝나 제목 줄이 아니므로 앞 줄과 한 문단으로 남는다
    assert [b.text for b in notes[1].blocks] == ["본문2 3. 이 회사는 기준서를 적용하고 있다."]
    assert warnings == []


def test_missing_first_inferred() -> None:
    """1 없이 2..5 연속 → 선두 블록을 주석 1(inferred) 로, 붕괴 없음."""
    warnings: list[str] = []
    notes = split_notes(_load("missing_first.html"), "단일", warnings)
    assert [(n.number, n.title, n.source) for n in notes] == [
        (1, "(제목 미확인)", "inferred"), (2, "중요한 회계정책", "text"), (3, "현금및현금성자산", "text"), (4, "매출채권", "text"), (5, "재고자산", "text")
    ]
    kinds = [b.kind for b in notes[0].blocks]
    assert kinds == ["paragraph", "paragraph", "paragraph", "table"]
    assert warnings == ["주석 1 제목을 찾지 못해 2번 이전 블록을 주석 1로 배정함 — 시트 주석01 확인 필요"]
    # expected_titles 가 있으면 1번 제목을 쓴다 (불일치 경고 없음)
    w2: list[str] = []
    notes2 = split_notes(_load("missing_first.html"), "단일", w2, ["1. 일반사항", "2. 중요한 회계정책", "3. 현금및현금성자산", "4. 매출채권", "5. 재고자산"])
    assert notes2[0].title == "일반사항" and notes2[0].source == "expected"
    assert not any("불일치" in w or "누락" in w for w in w2)
    assert note_sheet_name(notes[0]) == "주석01_제목미확인"


def test_only_one_note_still_collapses() -> None:
    """연속 검출이 2개 미만이면 여전히 주석00_미분류."""
    html = "<html><body><p>서문</p><p>2. 재고자산</p><p>본문</p></body></html>"
    warnings: list[str] = []
    notes = split_notes(html, "단일", warnings)
    assert len(notes) == 1 and notes[0].number == 0
    assert any("주석00_미분류" in w for w in warnings)


def test_truncated_bookmarktext_warning() -> None:
    """bookmarktext 가 정확히 20자이고 span 내부 텍스트가 비어 있으면 경고 1줄."""
    long20 = "1. 일반사항에관한아주긴제목입니다하나"  # 20자
    assert len(long20) == 20
    html = (
        f'<html><body><p><span bookmarktext="{long20}" id="bookmark_1"></span></p><p>본문</p>'
        '<p><span bookmarktext="2. 재고자산" id="bookmark_2">2. 재고자산</span></p><p>본문2</p></body></html>'
    )
    warnings: list[str] = []
    notes = split_notes(html, "단일", warnings)
    assert [n.number for n in notes] == [1, 2]
    assert notes[0].title == long20[3:]
    assert [w for w in warnings if "잘렸을 수 있음" in w] == [
        f"주석 1 제목이 잘렸을 수 있음 (bookmarktext 20자, 내부 텍스트 없음): {long20[3:]!r}"
    ]


# ---------- sheet name ----------


@pytest.mark.parametrize(
    "number, title, prefix, expected",
    [
        (1, "일반적 사항 (연결)", "연결", "연결주석01_일반적사항"),
        (21, "우발부채와 약정사항", "", "주석21_우발부채와약정사항"),
        (14, "순확정급여부채(자산) (연결)", "별도", "별도주석14_순확정급여부채"),
        (9, "종속기업, 관계기업 및 공동기업 투자", "", "주석09_종속기업관계기업및공동기업투자"),
        (2, "재무제표 작성기준 및 유의적인 회계정책과 중요한 회계추정", "연결", "연결주석02_재무제표작성기준및유의적인회계정책과중요한회계추"),  # 24자로 절단
        (0, "미분류", "", "주석00_미분류"),
    ],
)
def test_note_sheet_name(number: int, title: str, prefix: str, expected: str) -> None:
    name = note_sheet_name(Note(number=number, title=title, scope="단일"), prefix)
    assert name == expected
    assert len(name) <= 31
