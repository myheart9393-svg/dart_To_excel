"""dart.statements 테스트: 실데이터 fixture 3종 + 합성 fixture(tests/fixtures/statements/)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from dart.models import Statement, Table
from dart.statements import extract_statements, identify_statement_kind, merge_split_statement, validate_balance_sheet

STMT_DIR = Path(__file__).parent / "fixtures" / "statements"
ORDER = ["재무상태표", "손익계산서", "포괄손익계산서", "자본변동표", "현금흐름표"]


def _load(name: str) -> str:
    return (STMT_DIR / name).read_text(encoding="utf-8")


# ---------- identify_statement_kind ----------


@pytest.mark.parametrize(
    "ctx, expected",
    [
        (["2-1. 연결 재무상태표", "연결 재무상태표", "제 57 기 2025.12.31 현재"], "재무상태표"),
        (["연 결 포 괄 손 익 계 산 서"], "포괄손익계산서"),
        (["연 결 손 익 계 산 서"], "손익계산서"),
        (["연결현금흐름표", "제4(당)기"], "현금흐름표"),
        (["2-4. 연결 자본변동표"], "자본변동표"),
        (["2. 연결재무제표", "손익계산서", "포괄손익계산서"], "포괄손익계산서"),  # 뒤에서부터 첫 매칭
        (["회사명", "(단위 : 원)"], None),
    ],
)
def test_identify_by_title(ctx: list[str], expected) -> None:
    kind, basis = identify_statement_kind(ctx)
    assert kind == expected
    assert basis == ("제목" if expected else "")


def test_identify_by_content() -> None:
    bs = Table(header_rows=[["과목", "당기"]], rows=[["자산총계", 1.0], ["부채총계", 1.0]])
    assert identify_statement_kind([], bs) == ("재무상태표", "내용")
    cf = Table(header_rows=[["과목", "당기"]], rows=[["영업활동으로 인한 현금흐름", 1.0], ["당기순이익", 1.0]])
    assert identify_statement_kind([], cf) == ("현금흐름표", "내용")  # 당기순이익보다 현금흐름 우선
    eq = Table(header_rows=[["", "자본금", "이익잉여금", "합계"]], rows=[["기초", 1.0, 1.0, 1.0]])
    assert identify_statement_kind([], eq) == ("자본변동표", "내용")
    cis = Table(header_rows=[["과목", "당기"]], rows=[["당기순이익", 1.0], ["총포괄손익", 1.0]])
    assert identify_statement_kind([], cis) == ("포괄손익계산서", "내용")
    is_ = Table(header_rows=[["과목", "당기"]], rows=[["매출액", 1.0], ["영업이익", 1.0]])
    assert identify_statement_kind([], is_) == ("손익계산서", "내용")
    assert identify_statement_kind([], Table(rows=[["서명", "홍길동"]])) == (None, "")


# ---------- real data: 사업보고서 ----------


@pytest.mark.parametrize(
    "fixture, scope, first_title, equity_header_rows",
    [
        ("fs_annual_consolidated.html", "연결", "연결 재무상태표", 3),  # 자본 > 지배기업지분/비지배지분 > 항목
        ("fs_annual_separate.html", "별도", "재무상태표", 2),  # 자본 > 항목 (비지배지분 없음)
    ],
)
def test_annual(load_fixture, fixture: str, scope: str, first_title: str, equity_header_rows: int) -> None:
    warnings: list[str] = []
    stmts = extract_statements(load_fixture(fixture), scope, warnings)
    assert [s.kind for s in stmts] == ORDER
    assert all(s.scope == scope and s.basis == "제목" and s.suffix == "" for s in stmts)
    assert all(s.unit == "백만원" for s in stmts)
    assert all(s.period_text and "제 57 기" in s.period_text for s in stmts)
    assert stmts[0].title == first_title
    assert len(stmts[3].table.header_rows) == equity_header_rows  # 자본변동표 다단 헤더
    assert all(len(s.table.rows) == len(s.table.depths) for s in stmts)
    assert warnings == []


# ---------- real data: 감사보고서 (주석 포함 응답 → 절단) ----------


def test_audit_cut(load_fixture, caplog: pytest.LogCaptureFixture) -> None:
    warnings: list[str] = []
    with caplog.at_level(logging.INFO, logger="dart.statements"):
        stmts = extract_statements(load_fixture("fs_audit.html"), "연결", warnings)
    kinds = [s.kind for s in stmts]
    assert 4 <= len(stmts) <= 5
    assert kinds == ["재무상태표", "포괄손익계산서", "자본변동표", "현금흐름표"]
    assert all(s.basis == "제목" and s.unit == "원" for s in stmts)
    # 절단이 작동: 주석 표가 섞이지 않음
    for s in stmts:
        assert not any(str(r[0]).startswith("1. 일반") for r in s.table.rows)
        assert "주석" not in s.title
    cut_msgs = [r.getMessage() for r in caplog.records if "주석 시작 감지" in r.getMessage()]
    assert len(cut_msgs) == 1 and "표 143개 미처리" in cut_msgs[0]
    # 처리한 본문표 ≤ 12 (경고의 표 순번은 본문표 기준이므로 간접 확인)
    assert all("표 순번" not in w or int(w.rsplit("표 순번 ", 1)[1]) <= 12 for w in warnings)
    bs = stmts[0]
    assert bs.title == "연 결 재 무 상 태 표"
    # 원문 <thead> 는 1행뿐 (과목 | 주석 | 제 4(당) 기말 colspan=2 | 제 3(전) 기말 colspan=2) → 1행 6열로 전개
    assert bs.table.header_rows == [["과 목", "주 석", "제 4(당) 기말", "제 4(당) 기말", "제 3(전) 기말", "제 3(전) 기말"]]
    row = next(r for r in bs.table.rows if r[0] == "현금및현금성자산")
    assert row[1] == "4,5,6,7" and isinstance(row[2], float) and row[3] is None  # 주석 열 문자열, 소계/합계 2열 유지
    assert "제 4(당)기말" in (bs.period_text or "")


# ---------- synthetic ----------


def test_no_title_content_based() -> None:
    warnings: list[str] = []
    stmts = extract_statements(_load("no_title_bs.html"), "단일", warnings)
    assert len(stmts) == 1
    s = stmts[0]
    assert s.kind == "재무상태표" and s.basis == "내용" and s.title == "재무상태표"
    assert s.unit == "천원" and s.period_text == "제 3 기 2025.12.31 현재"
    assert warnings == ["제목 없이 내용 기반 분류: 재무상태표, 표 순번 1"]


def test_split_same_header_merged(caplog: pytest.LogCaptureFixture) -> None:
    warnings: list[str] = []
    with caplog.at_level(logging.INFO, logger="dart.statements"):
        stmts = extract_statements(_load("split_same_header.html"), "단일", warnings)
    assert len(stmts) == 1
    s = stmts[0]
    assert s.kind == "재무상태표" and s.suffix == "" and s.title == "재 무 상 태 표"
    assert [r[0] for r in s.table.rows] == ["자산", "유동자산", "자산총계", "부채", "부채총계", "부채와자본총계"]
    assert s.table.depths == [0, 1, 1, 0, 1, 0]
    assert any("병합" in w for w in warnings)
    assert any("주석 시작 감지" in r.getMessage() and "표 1개 미처리" in r.getMessage() for r in caplog.records)


def test_split_diff_header_suffix() -> None:
    warnings: list[str] = []
    stmts = extract_statements(_load("split_diff_header.html"), "단일", warnings)
    assert [(s.kind, s.suffix) for s in stmts] == [("손익계산서", ""), ("손익계산서", "_2")]
    assert stmts[1].table.header_rows == [["과목", "당기 3개월", "전기 3개월"]]
    assert stmts[1].period_text == "제 3 기 4분기 3개월"
    assert any("_2" in w for w in warnings)


def test_merge_split_statement_rejects_different_header() -> None:
    a = Statement("재무상태표", "단일", "t", None, None, Table(header_rows=[["a"]], rows=[["x", 1.0]], depths=[0]))
    b = Statement("재무상태표", "단일", "t", None, None, Table(header_rows=[["b"]], rows=[["y", 2.0]], depths=[0]))
    with pytest.raises(ValueError):
        merge_split_statement(a, b)
    b.table.header_rows = [["a"]]
    m = merge_split_statement(a, b)
    assert m.table.rows == [["x", 1.0], ["y", 2.0]] and m.table.depths == [0, 0]
    assert a.table.rows == [["x", 1.0]]  # 원본 불변



# ---------- validate_balance_sheet ----------


def _bs_stmt(rows, header=None, scope="단일") -> Statement:
    return Statement("재무상태표", scope, "재무상태표", None, "원", Table(header_rows=header or [["과목", "당기", "전기"]], rows=rows))  # type: ignore[arg-type]


def test_validate_balance_ok_and_mismatch() -> None:
    w: list[str] = []
    validate_balance_sheet(_bs_stmt([["자산총계", 1000.0, 900.0], ["부채총계", 400.0, 350.0], ["자본총계", 600.0, 550.0]]), w)
    assert w == []
    validate_balance_sheet(_bs_stmt([["자산총계", 1000.0, 900.0], ["부채총계", 400.0, 350.0], ["자본총계", 590.0, 550.0]]), w)
    assert len(w) == 1 and "당기: 자산총계 1,000 ≠ 부채 400 + 자본 590 (차이 10) — 파싱 오류 가능" in w[0]


def test_validate_balance_total_le_and_missing() -> None:
    w: list[str] = []
    validate_balance_sheet(_bs_stmt([["자산 총계", 1000.0, 900.0], ["부채와자본총계", 1000.0, 905.0]]), w)
    assert len(w) == 1 and "전기" in w[0] and "부채와자본총계 905" in w[0] and "차이 5" in w[0]
    w2: list[str] = []
    validate_balance_sheet(_bs_stmt([["자산총계", 1.0, 1.0]]), w2)
    assert w2 == ["재무상태표(단일) 대차 검증 불가: 부채총계, 자본총계 행 없음"]
    w3: list[str] = []  # 주석 열·None 열은 건너뜀
    validate_balance_sheet(
        _bs_stmt([["자산총계", "4", None, 100.0], ["부채총계", "5", None, 40.0], ["자본총계", "", None, 60.0]], [["과목", "주석", "소계", "합계"]]), w3
    )
    assert w3 == []


@pytest.mark.parametrize(
    "fixture, scope", [("fs_annual_consolidated.html", "연결"), ("fs_annual_separate.html", "별도"), ("fs_audit.html", "연결"), ("fs_audit_separate.html", "단일")]
)
def test_real_balance_validation_passes(load_fixture, fixture: str, scope: str) -> None:
    warnings: list[str] = []
    stmts = extract_statements(load_fixture(fixture), scope, warnings)
    assert any(s.kind == "재무상태표" for s in stmts)
    assert not any("대차" in w or "파싱 오류 가능" in w for w in warnings), warnings


def test_audit_separate_statements(load_fixture) -> None:
    warnings: list[str] = []
    stmts = extract_statements(load_fixture("fs_audit_separate.html"), "단일", warnings)
    assert [s.kind for s in stmts] == ["재무상태표", "손익계산서", "자본변동표", "현금흐름표"]  # 포괄손익계산서 없음(일반기업회계기준)
    assert all(s.scope == "단일" and s.basis == "제목" for s in stmts)
    assert stmts[0].table.header_rows == [["과 목", "제 16(당) 기", "제 16(당) 기", "제 15(전) 기", "제 15(전) 기"]]
    assert warnings == []


def test_cut_at_branch_first_note() -> None:
    """주석이 ``1-1.`` 로 시작하는 문서에서도 절단이 동작한다 (가지 번호 정규식 확장 후)."""
    html = (
        '<html><body>'
        '<table class="nb"><tr><td>재 무 상 태 표</td></tr><tr><td>(단위 : 원)</td></tr></table>'
        '<table border="1"><thead><tr><th>과목</th><th>당기</th></tr></thead><tbody>'
        '<tr><td>자산총계</td><td>100</td></tr><tr><td>부채총계</td><td>40</td></tr>'
        '<tr><td>자본총계</td><td>60</td></tr></tbody></table>'
        '<p>1-1. 일반사항</p>'
        '<table border="1"><thead><tr><th>구분</th><th>당기</th></tr></thead><tbody>'
        '<tr><td>자산총계</td><td>1</td></tr><tr><td>부채총계</td><td>1</td></tr></tbody></table>'
        '</body></html>'
    )
    warnings: list[str] = []
    stmts = extract_statements(html, "단일", warnings)
    assert [s.kind for s in stmts] == ["재무상태표"]
    assert len(stmts[0].table.rows) == 3  # 주석 쪽 표는 절단되어 미포함


def test_mixed_numeric_warning_aggregated() -> None:
    """재무제표 표의 숫자 열 문자열 혼입은 표별 경고 대신 1줄 집계 (주석과 동일 방식)."""
    html = (
        '<html><body><table class="nb"><tr><td>재 무 상 태 표</td></tr></table>'
        '<table border="1"><thead><tr><th>과목</th><th>제 1 기</th><th>제 2 기</th></tr></thead>'
        '<tr><td>자산총계</td><td>100</td><td>비지배*</td></tr>'
        '<tr><td>부채총계</td><td>60</td><td>50</td></tr>'
        '<tr><td>자본총계</td><td>40</td><td>해당없음</td></tr>'
        '</table></body></html>'
    )
    warnings: list[str] = []
    statements = extract_statements(html, "단일", warnings)
    assert len(statements) == 1 and statements[0].kind == "재무상태표"
    mixed = [w for w in warnings if "숫자" in w]
    assert mixed == [
        "재무제표 표 1개에서 숫자 열에 문자열 값이 섞여 원문 그대로 남김 (재무상태표)"
    ]


def _stmt_with_rows(rows: list[list]) -> Statement:
    table = Table(header_rows=[["과목", "제 1 기"]], rows=rows, depths=[0] * len(rows))
    return Statement(kind="재무상태표", scope="단일", title="재무상태표", period_text=None, unit=None, table=table)


def _load_fx(name: str):
    from pathlib import Path
    return (Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8")


def test_total_row_paren_priority() -> None:
    """'자본총계(지배기업소유주지분)' 이 있어도 괄호 없는 '자본총계'(비지배지분 포함) 행을 쓴다 (실측 KX)."""
    stmt = _stmt_with_rows([
        ["자산총계", 150.0],
        ["부채총계", 50.0],
        ["자본총계(지배기업소유주지분)", 80.0],
        ["비지배지분", 20.0],
        ["자본총계", 100.0],
    ])
    warnings: list[str] = []
    validate_balance_sheet(stmt, warnings)
    assert warnings == []


def test_total_row_paren_priority_real() -> None:
    """KX 반기 연결 재무제표: 대차 경고 없음."""
    warnings: list[str] = []
    extract_statements(_load_fx("fs_half_nci_paren.html"), "연결", warnings)
    assert not any("파싱 오류 가능" in w or "검증 불가" in w for w in warnings)


def test_total_row_chongja_real() -> None:
    """신세계 별도 재무제표: '총자산'/'총부채' 키로 검증 통과."""
    warnings: list[str] = []
    extract_statements(_load_fx("fs_annual_chongja.html"), "별도", warnings)
    assert not any("파싱 오류 가능" in w or "검증 불가" in w for w in warnings)


def test_dormant_body_table_relaxed() -> None:
    """휴면회사(값이 거의 '-')의 border=1 본문표: 숫자 비율 미달이어도 추출된다 (실측 네오슈테른)."""
    warnings: list[str] = []
    statements = extract_statements(_load_fx("fs_annual_dormant.html"), "별도", warnings)
    kinds = {s.kind for s in statements}
    assert kinds == {"재무상태표", "손익계산서"}
    bs = next(s for s in statements if s.kind == "재무상태표")
    vals = [v for r in bs.table.rows for v in r[1:]]
    assert sum(1 for v in vals if v is None) > len(vals) / 2  # 휴면회사: 값 대부분 빈 값
