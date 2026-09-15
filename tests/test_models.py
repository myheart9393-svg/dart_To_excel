"""dart.models dataclass 생성·기본값 테스트."""

from __future__ import annotations

from dart.models import DocNode, Note, NoteBlock, ParsedReport, Statement, Table


def test_docnode_defaults() -> None:
    node = DocNode(
        title="4. 재무제표",
        rcp_no="20240321000123",
        dcm_no="9876543",
        ele_id="15",
        offset="123",
        length="456",
        dtd="dart3.xsd",
        url="https://dart.fss.or.kr/report/viewer.do?rcpNo=20240321000123",
    )
    assert node.depth == 0
    assert node.parent_title is None


def test_table_defaults_are_independent() -> None:
    t1 = Table()
    t2 = Table()
    t1.rows.append(["현금", 1.0])
    assert t2.rows == []
    assert t1.caption is None and t1.unit is None


def test_statement_holds_table() -> None:
    table = Table(header_rows=[["과목", "제 55 기", "제 54 기"]], rows=[["자산", 1.0, None]])
    stmt = Statement(
        kind="재무상태표",
        scope="연결",
        title="연 결 재 무 상 태 표",
        period_text="제 55 기 2023.12.31 현재",
        unit="(단위 : 원)",
        table=table,
    )
    assert stmt.table.rows[0][1] == 1.0
    assert stmt.table.rows[0][2] is None


def test_note_and_blocks() -> None:
    note = Note(number=21, title="우발부채와 약정사항", scope="단일")
    note.blocks.append(NoteBlock(kind="subheading", text="(1) 담보제공자산"))
    note.blocks.append(NoteBlock(kind="table", table=Table(unit="(단위: 천원)")))
    assert [b.kind for b in note.blocks] == ["subheading", "table"]
    assert note.blocks[1].table is not None and note.blocks[1].text is None


def test_parsed_report_defaults() -> None:
    report = ParsedReport()
    assert report.meta == {}
    assert report.statements == []
    assert report.notes == []
    assert report.warnings == []
    report.warnings.append("경고")
    assert ParsedReport().warnings == []
