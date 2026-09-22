"""배치 2차 조치 테스트: 트리 제목 정확 일치 0단계(G)와 대차 검증 fallback(F).

fixtures: 휴맥스 20250325000222 (점 가지 ``2.1``·복수 번호 ``19, 20.`` 자식 제목),
현대리바트 20260316001287 연결 재무제표 (XBRL 표준계정명 — ``자산``/``자본과 부채``, 총계 행 없음).
"""

from __future__ import annotations

from pathlib import Path

from dart.excel import note_label, note_sheet_name
from dart.models import Statement, Table
from dart.notes import parse_expected_heading, split_notes
from dart.statements import extract_statements, validate_balance_sheet
from dart.tree import parse_doc_tree, select_target_nodes

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _expected_titles(main_fixture: str, rcp_no: str, key: str) -> list[str]:
    sel = select_target_nodes(parse_doc_tree(_load(main_fixture), rcp_no))
    return [c.title for c in sel[key].children]


# ---------- parse_expected_heading ----------


def test_parse_expected_heading_formats() -> None:
    """4형식: N. / N-M. / N.M / N, M. — (번호, 가지, 제목, 라벨)."""
    assert parse_expected_heading("1. 일반사항") == (1, None, "일반사항", "1")
    assert parse_expected_heading("14-1. 무형자산") == (14, 1, "무형자산", "14-1")
    assert parse_expected_heading("2.1 재무제표 작성기준 - 연결") == (2, 1, "재무제표 작성기준 - 연결", "2.1")
    assert parse_expected_heading("19, 20. 영업권 및 무형자산") == (19, None, "영업권 및 무형자산", "19, 20")
    assert parse_expected_heading("가. 개요") is None
    assert parse_expected_heading("주석") is None


def test_parse_expected_heading_prefix() -> None:
    """5형식: 주석N - 제목 / 주석N,M - 제목 / 주석 N. 제목 (실측 20260323001603 제이앤티씨)."""
    assert parse_expected_heading("주석1,2 - 회사의 개요 및 재무제표 작성기준 (연결)") == (
        1, None, "회사의 개요 및 재무제표 작성기준 (연결)", "1,2"
    )
    assert parse_expected_heading("주석3 - 회계정책의 변경") == (3, None, "회계정책의 변경", "3")
    assert parse_expected_heading("주석 4. 중요한 회계정책") == (4, None, "중요한 회계정책", "4")
    assert parse_expected_heading("주석5 영업부문") == (5, None, "영업부문", "5")
    # 이마트형 (실측 20260318001024): 주석 뒤에 하이픈이 온다
    assert parse_expected_heading("주석 - 1. 일반사항 - 연결 (연결)") == (1, None, "일반사항 - 연결 (연결)", "1")


def test_expected_prefix_dash_real() -> None:
    """이마트 별도주석('주석 - N. 제목' 트리·본문 동일): 자식 41개 전부 검출, 미분류 0, 경고 없음."""
    expected = _expected_titles("main_do_annual_noteprefix2.html", "20260318001024", "별도_주석")
    warnings: list[str] = []
    notes = split_notes(_load("notes_annual_noteprefix2.html"), "별도", warnings, expected)
    assert len(notes) == len(expected) == 41
    assert sum(1 for n in notes if n.number == 0) == 0
    assert all(n.source == "expected" for n in notes)
    assert [w for w in warnings if "숫자 열" not in w] == []


# ---------- 0단계: 트리 제목 정확 일치 ----------


def test_expected_exact_dotbranch_real() -> None:
    """휴맥스 연결주석: 자식 47개 전부 검출, 미분류 0, 비정형 라벨 보존, W_MIXED 외 경고 없음."""
    expected = _expected_titles("main_do_annual_dotbranch.html", "20250325000222", "연결_주석")
    warnings: list[str] = []
    notes = split_notes(_load("notes_annual_dotbranch.html"), "연결", warnings, expected)
    assert len(notes) == len(expected) == 47
    assert sum(1 for n in notes if n.number == 0) == 0
    assert all(n.source == "expected" for n in notes)
    labels = [note_label(n) for n in notes]
    assert "2.1" in labels and "19, 20" in labels and "42.3" in labels
    assert [w for w in warnings if "숫자 열" not in w] == []


def test_expected_exact_merged_title_real() -> None:
    """부산주공 별도주석: '34. …, 35. …' 합본 제목도 정확 일치로 검출, 누락·GAP 경고 없음."""
    expected = _expected_titles("main_do_annual_noconsol.html", "20260319000808", "별도_주석")
    warnings: list[str] = []
    notes = split_notes(_load("notes_annual_noconsol.html"), "별도", warnings, expected)
    assert len(notes) == len(expected) == 45
    merged = next(n for n in notes if n.number == 34)
    assert "35. 종업원급여" in merged.title
    assert [w for w in warnings if "숫자 열" not in w] == []


def test_expected_exact_giveup_falls_back() -> None:
    """기대 제목의 30% 이상을 못 찾으면 0단계를 포기하고 기존 단계로 진행 + 경고."""
    html = (
        "<html><body><p>1. 일반사항</p><p>본문</p>"
        "<p>2. 회계정책</p><p>본문</p><p>3. 현금</p><p>본문</p></body></html>"
    )
    warnings: list[str] = []
    notes = split_notes(html, "단일", warnings, ["1. 다른제목", "2. 엉뚱한제목", "3. 현금"])
    assert any("텍스트 단계로 fallback" in w for w in warnings)
    assert [n.number for n in notes] == [1, 2, 3]  # 기존 텍스트 단계가 처리


def test_expected_exact_missing_middle_warns() -> None:
    """중간 기대 제목 하나를 못 찾으면(30% 미만) 그 주석만 경고하고 나머지는 검출."""
    html = "".join(f"<p>{i}. 제목{i}</p><p>본문{i}</p>" for i in range(1, 11) if i != 5)
    warnings: list[str] = []
    notes = split_notes(f"<html><body>{html}</body></html>", "단일", warnings, [f"{i}. 제목{i}" for i in range(1, 11)])
    assert [n.number for n in notes] == [i for i in range(1, 11) if i != 5]
    assert any("주석 5 제목을 본문에서 찾지 못함" in w for w in warnings)
    assert not any("누락 (검출 순서상" in w for w in warnings)  # N_GAP 없음


def test_expected_prefix_real() -> None:
    """제이앤티씨 연결주석(주석N - 제목 트리): 자식 38개 전부 검출, 미분류 0, 경고 없음."""
    expected = _expected_titles("main_do_annual_noteprefix.html", "20260323001603", "연결_주석")
    warnings: list[str] = []
    notes = split_notes(_load("notes_annual_noteprefix.html"), "연결", warnings, expected)
    assert len(notes) == len(expected) == 38
    assert sum(1 for n in notes if n.number == 0) == 0
    assert note_label(notes[0]) == "1,2"
    assert note_sheet_name(notes[0], "연결").startswith("연결주석01-02_")
    assert [w for w in warnings if "숫자 열" not in w] == []


def test_dot_ending_title_real() -> None:
    """타이코화이어 연결주석: '11. 법인세.' 가 마침표 종결 예외로 검출되어 N_GAP 없음."""
    warnings: list[str] = []
    notes = split_notes(_load("notes_audit_dottitle.html"), "연결", warnings)
    nums = [n.number for n in notes]
    assert 11 in nums and 10 in nums and 12 in nums
    assert not any("주석 11 누락" in w for w in warnings)


def test_dot_ending_title_synthetic() -> None:
    """마침표 종결 완화의 한계: 공백 있는 문장('이 회사는 있다.')은 여전히 탈락."""
    html = (
        "<html><body><p>1. 개요</p><p>본문</p><p>2. 정책</p><p>본문</p>"
        "<p>3. 이 회사는 있다.</p><p>본문</p></body></html>"
    )
    warnings: list[str] = []
    notes = split_notes(html, "단일", warnings)
    assert [n.number for n in notes] == [1, 2]
    # 짧은 명사형 마침표 제목은 허용
    html2 = "<html><body><p>1. 개요</p><p>본문</p><p>2. 법인세.</p><p>본문</p></body></html>"
    notes2 = split_notes(html2, "단일", [])
    assert [n.number for n in notes2] == [1, 2] and notes2[1].title == "법인세."


# ---------- 시트명·라벨 ----------


def test_note_label_and_sheet_name_from_label() -> None:
    """label 이 있으면 시트명은 label 기준: 02.1, 19-20 (쉼표 → 하이픈)."""
    from dart.models import Note

    dot = Note(number=2, title="재무제표 작성기준", scope="연결", branch=1, label="2.1")
    comma = Note(number=19, title="영업권 및 무형자산", scope="연결", label="19, 20")
    assert note_label(dot) == "2.1" and note_label(comma) == "19, 20"
    assert note_sheet_name(dot, "연결") == "연결주석02.1_재무제표작성기준"
    assert note_sheet_name(comma, "연결") == "연결주석19-20_영업권및무형자산"


# ---------- 대차 검증 fallback ----------


def _bs(rows: list[list]) -> Statement:
    table = Table(header_rows=[["과목", "제 1 기"]], rows=rows, depths=[0] * len(rows))
    return Statement(kind="재무상태표", scope="단일", title="재무상태표", period_text=None, unit=None, table=table)


def test_balance_fallback_synthetic() -> None:
    """총계 행이 없어도 '자산'/'부채'/'자본' 값 행으로 검증. 헤더성(값 없음) 행은 제외."""
    warnings: list[str] = []
    validate_balance_sheet(
        _bs([["자산", None], ["현금", 40.0], ["자산", 100.0], ["부채", 60.0], ["자본", 40.0], ["자본과 부채", 100.0]]),
        warnings,
    )
    assert warnings == []
    # 값이 어긋나면 여전히 경고
    warnings2: list[str] = []
    validate_balance_sheet(_bs([["자산", 100.0], ["부채", 50.0], ["자본", 40.0]]), warnings2)
    assert any("파싱 오류 가능" in w for w in warnings2)
    # fallback 행조차 없으면 기존 '검증 불가' 경고
    warnings3: list[str] = []
    validate_balance_sheet(_bs([["현금", 40.0]]), warnings3)
    assert any("대차 검증 불가" in w for w in warnings3)


def test_balance_fallback_xbrl_real() -> None:
    """현대리바트 연결 재무제표(XBRL 표준계정명): fallback 으로 검증 통과, 대차 경고 없음."""
    warnings: list[str] = []
    statements = extract_statements(_load("fs_annual_xbrl.html"), "연결", warnings)
    kinds = {s.kind for s in statements}
    assert "재무상태표" in kinds and len(statements) == 4
    assert not any("대차 검증 불가" in w or "파싱 오류 가능" in w for w in warnings)
