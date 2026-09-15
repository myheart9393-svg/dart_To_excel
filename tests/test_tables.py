"""dart.tables 테스트: 엣지 케이스 fixture(tests/fixtures/tables/) + 실데이터 재무제표."""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from dart.tables import (
    count_thead_rows,
    extract_unit,
    grid_to_table,
    html_table_to_grid,
    normalize_number,
    split_header_rows,
)

TABLES_DIR = Path(__file__).parent / "fixtures" / "tables"


def _table(name: str, index: int = 0):
    soup = BeautifulSoup((TABLES_DIR / name).read_text(encoding="utf-8"), "lxml")
    return soup.find_all("table")[index]


def _real_tables(load_fixture, name: str):
    return BeautifulSoup(load_fixture(name), "lxml").find_all("table")


# ---------- normalize_number ----------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("(1,234)", -1234.0),
        ("-1,234", -1234.0),
        ("△1,234", -1234.0),
        ("▲1,234", -1234.0),
        ("(1,234.5)", -1234.5),
        ("1,234", 1234.0),
        ("1234", 1234.0),
        ("1,234.5", 1234.5),
        ("1,234.", 1234.0),
        ("0", 0.0),
        (" 12 ", 12.0),
        ("-", None),
        ("—", None),
        ("－", None),
        ("", None),
        ("　", None),
        ("해당없음", None),
        ("해당사항없음", None),
        ("１，２３４", 1234.0),
        ("（５６７）", -567.0),
        ("４,５", "４,５"),  # 숫자 아님(주석 참조) → 원문
        ("4,12", "4,12"),
        ("주4,29", "주4,29"),
        ("12.5%", "12.5%"),
        ("2025.12.31", "2025.12.31"),
        ("1,234원", "1,234원"),
    ],
)
def test_normalize_number(raw: str, expected) -> None:
    got = normalize_number(raw)
    assert got == expected
    if isinstance(expected, float):
        assert isinstance(got, float)


# ---------- extract_unit ----------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("(단위 : 원)", "원"),
        ("(단위: 백만원)", "백만원"),
        ("(단위 : 천원, 천USD)", "천원, 천USD"),
        ("단위:원", "원"),
        ("제 57 기 2025.12.31 현재 （단위：백만원）", "백만원"),
        ("(단위 천원)", "천원"),
        ("단위당 원가는 다음과 같습니다", None),
        ("", None),
    ],
)
def test_extract_unit(raw: str, expected) -> None:
    assert extract_unit(raw) == expected


# ---------- edge-case fixtures ----------


def test_header_2rows() -> None:
    t = _table("header_2rows.html")
    grid, depth = html_table_to_grid(t, return_depth=True)
    assert count_thead_rows(t) == 2
    assert grid[0] == ["과목", "제 57 기", "제 57 기", "제 56 기", "제 56 기"]
    assert grid[1] == ["과목", "당기", "누적", "전기", "누적"]  # rowspan 복사
    assert len(grid) == 5 and all(len(r) == 5 for r in grid)
    assert [d[0] for d in depth] == [0, 0, 0, 1, 2]
    w: list[str] = []
    table = grid_to_table(grid, depth, split_header_rows(grid, 2), "손익", "원", w)
    assert table.header_rows == grid[:2]
    assert table.rows[0] == ["매출액", 1000.0, 4000.0, 900.0, 3600.0]
    assert table.rows[1] == ["매출원가", -600.0, -2400.0, -500.0, -2000.0]
    assert table.rows[2] == ["기타", None, 10.0, -5.0, 0.0]
    assert table.depths == [0, 1, 2]
    assert w == []


def test_equity_wide() -> None:
    t = _table("equity_wide.html")
    grid, depth = html_table_to_grid(t, return_depth=True)
    assert count_thead_rows(t) == 3
    assert all(len(r) == 8 for r in grid) and len(grid) == 7
    assert grid[0] == ["", "자본", "자본", "자본", "자본", "자본", "자본", "자본"]
    assert grid[1] == ["", "지배기업의 소유주지분"] * 1 + ["지배기업의 소유주지분"] * 4 + ["비지배지분", "자본 합계"]
    assert grid[2] == ["", "자본금", "주식발행초과금", "이익잉여금", "기타자본항목", "소유주지분 합계", "비지배지분", "자본 합계"]
    w: list[str] = []
    table = grid_to_table(grid, depth, 3, "자본변동표", None, w)
    assert len(table.header_rows) == 3 and len(table.rows) == 4
    assert table.rows[-1] == ["2025.12.31 (기말자본)", 100.0, 200.0, 350.0, -40.0, 610.0, 55.0, 665.0]
    assert table.rows[2][3] == -20.0
    assert w == []


def test_broken_rows() -> None:
    t = _table("broken_rows.html")
    pad_w: list[str] = []
    grid = html_table_to_grid(t, warnings=pad_w, table_no=3)
    assert pad_w == ["표 3: 열 수 불일치(최대 4, 최소 1)로 패딩"]
    assert html_table_to_grid(_table("header_2rows.html"), warnings=(no_pad := [])) and no_pad == []
    # 빈 tr, td 없는 tr 은 건너뜀 → 4행, 최대 4열로 패딩
    assert len(grid) == 4
    assert all(len(r) == 4 for r in grid)
    assert grid[0] == ["과목", "당기", "전기", ""]
    assert grid[1] == ["자산", "", "", ""]
    assert grid[2] == ["유동자산", "1,234", "1,000", "여분"]
    assert grid[3] == ["자산총계", "1,234", "", ""]
    assert count_thead_rows(t) == 0
    assert split_header_rows(grid) == 1  # 휴리스틱: 첫 행만 헤더
    # grid_to_table 은 ragged 입력을 조용히 패딩하고, 문자열 혼입만 경고한다
    ragged = [["과목", "당기"], ["자산총계", "1,234", "x"], ["부채", "abc", "7"]]
    w: list[str] = []
    table = grid_to_table(ragged, None, 1, "깨진표", None, w)
    assert table.rows == [["자산총계", 1234.0, "x"], ["부채", "abc", 7.0]]
    assert len(w) == 2 and all("문자열" in m and "패딩" not in m for m in w)  # 숫자·문자열이 섞인 두 열
    # 전부 문자열인 열(텍스트 열)은 경고하지 않는다
    w2: list[str] = []
    grid_to_table([["지역", "기업명", "지분율"], ["미주", "SEA", "100"], ["구주", "SEUK", "100"]], None, 1, "종속기업", None, w2)
    assert w2 == []


def test_nested_table() -> None:
    t = _table("nested_table.html")
    grid = html_table_to_grid(t)
    assert len(grid) == 3  # 안쪽 표의 tr 은 세지 않음
    assert grid[1][0] == "내역 안쪽A 안쪽B 안쪽C"
    assert grid[1][1] == "1,000"
    assert grid[2] == ["합계", "1,000"]


def test_br_and_fullwidth() -> None:
    t = _table("br_and_fullwidth.html")
    grid, depth = html_table_to_grid(t, return_depth=True)
    assert grid[1] == ["자산", "", "", ""]  # <br/> 만 있는 셀은 빈 문자열
    assert grid[2][0] == "현금및 현금성자산"  # <br> → 공백
    assert [d[0] for d in depth] == [0, 0, 1, 1, 2]  # &nbsp; &nbsp; 반복 → 폭 순위
    w: list[str] = []
    table = grid_to_table(grid, depth, split_header_rows(grid, count_thead_rows(t)), None, "원", w)
    assert table.header_rows == [["과 목", "주 석", "제 4(당) 기말", "제 3(전) 기말"]]
    assert table.rows[1] == ["현금및 현금성자산", "4,12", 1234.0, -1234.0]  # 주석 열은 문자열 유지
    assert table.rows[2] == ["단기금융상품", "4", -567.0, None]
    assert table.rows[3] == ["소계", "", None, 1234.5]
    assert table.depths == [0, 1, 1, 2]
    assert w == []


def test_split_header_rows_heuristic() -> None:
    grid = [["", "제 57 기", "제 56 기"], ["", "당기", "전기"], ["자산", "1", "2"]]
    assert split_header_rows(grid) == 2
    assert split_header_rows(grid, thead_rows=1) == 1
    grid2 = [["과목", "당기"], ["자산", ""], ["유동자산", "1"]]
    assert split_header_rows(grid2) == 1  # 2행은 첫 열 외 모두 비어 있어 헤더 아님
    assert split_header_rows([["a", "b"]] * 5) == 3  # 최대 3


def test_full_span_section_row() -> None:
    """전체 폭 colspan 구분 행은 숫자 열에 복사된 라벨을 None 으로 바꾸고 경고하지 않는다."""
    grid = [
        ["과목", "자본금", "결손금", "합계"],
        ["총포괄손익:", "총포괄손익:", "총포괄손익:", "총포괄손익:"],
        ["당기순손실", "0", "(100)", "(100)"],
        ["", "", "", ""],
    ]
    w: list[str] = []
    table = grid_to_table(grid, None, 1, "자본변동표", None, w)
    assert table.rows[0] == ["총포괄손익:", None, None, None]
    assert table.rows[1] == ["당기순손실", 0.0, -100.0, -100.0]
    assert table.rows[2] == ["", None, None, None]
    assert w == []


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("현금및현금성자산 (주4,29)", ("현금및현금성자산", "4,29")),
        ("재고자산 (주8)", ("재고자산", "8")),
        ("매출액 (주30)", ("매출액", "30")),
        ("단기당기손익-공정가치금융자산 (주4,6,29)", ("단기당기손익-공정가치금융자산", "4,6,29")),
        ("현금및현금성자산(주석3, 20)", ("현금및현금성자산", "3,20")),
        ("차입금 (주석 3~5)", ("차입금", "3~5")),
        ("재고자산（주4）", ("재고자산", "4")),
        ("유형자산 (주 4, 29)", ("유형자산", "4,29")),
        ("자산총계", ("자산총계", None)),
        ("(주4) 별도 문단", ("(주4) 별도 문단", None)),
    ],
)
def test_split_note_ref(raw: str, expected) -> None:
    from dart.tables import split_note_ref

    assert split_note_ref(raw) == expected


# ---------- real data ----------


def test_real_balance_sheet(load_fixture) -> None:
    tables = _real_tables(load_fixture, "fs_annual_consolidated.html")
    assert len(tables) == 10
    bs = tables[1]  # 표 1 = 제목표(class=nb), 표 2 = 연결 재무상태표 본문
    grid, depth = html_table_to_grid(bs, return_depth=True)
    w: list[str] = []
    table = grid_to_table(grid, depth, split_header_rows(grid, count_thead_rows(bs)), "연결 재무상태표", "백만원", w)
    assert table.header_rows == [["", "제 57 기", "제 56 기", "제 55 기"]]
    names = [r[0] for r in table.rows]
    assert "자산총계" in names
    total = table.rows[names.index("자산총계")]
    assert isinstance(total[1], float) and total[1] > 0
    d = dict(zip(names, table.depths))
    assert d["자산"] == 0
    assert d["유동자산"] == 1
    assert d["현금및현금성자산"] == 2  # "(주4,29)" 는 note_refs 로 분리
    refs = dict(zip(names, table.note_refs))
    assert refs["현금및현금성자산"] == "4,29" and refs["유동자산"] is None and refs["재고자산"] == "8"
    assert d["자산총계"] == 1  # 원문이 "　자산총계" (전각공백 1개) — 유동자산과 같은 수준
    assert d["부채와자본총계"] == 0
    assert d["우선주자본금"] == 3
    assert len(table.depths) == len(table.rows)
    assert w == []


def test_real_equity_statement(load_fixture) -> None:
    tables = _real_tables(load_fixture, "fs_annual_consolidated.html")
    eq = tables[7]
    grid, depth = html_table_to_grid(eq, return_depth=True)
    assert count_thead_rows(eq) == 3
    assert all(len(r) == 8 for r in grid)
    assert grid[0][1:] == ["자본"] * 7
    assert grid[2][1] == "자본금" and grid[2][-1] == "자본 합계"
    w: list[str] = []
    table = grid_to_table(grid, depth, 3, "연결 자본변동표", "백만원", w)
    assert table.rows[0][0].endswith("(기초자본)")
    assert all(isinstance(v, (float, type(None))) for v in table.rows[0][1:])


def test_real_audit_fs_table_count(load_fixture) -> None:
    """fs_audit.html: viewer.do 가 length 를 무시해 주석까지 포함된 상태 (표 160개)."""
    tables = _real_tables(load_fixture, "fs_audit.html")
    assert len(tables) == 160
    bs = tables[6]
    grid, depth = html_table_to_grid(bs, return_depth=True)
    w: list[str] = []
    table = grid_to_table(grid, depth, split_header_rows(grid, count_thead_rows(bs)), "연결재무상태표", "원", w)
    assert table.header_rows[0][:2] == ["과 목", "주 석"]
    row = next(r for r in table.rows if r[0] == "현금및현금성자산")
    assert row[1] == "4,5,6,7"  # 주석 열은 문자열
    assert isinstance(row[2], float)
    assert table.depths[table.rows.index(row)] == 1
