"""모듈 간 공유 dataclass 정의.

다른 모듈은 서로를 직접 import 하지 않고 이 모듈의 타입만으로 통신한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional, Union

# 재무제표 종류. 손익계산서와 포괄손익계산서는 별개 시트이므로 별개 kind.
StatementKind = Literal["재무상태표", "손익계산서", "포괄손익계산서", "자본변동표", "현금흐름표"]

# 연결/별도 구분. 둘 중 하나만 있으면 "단일" (시트명에 접두어를 붙이지 않음).
Scope = Literal["연결", "별도", "단일"]

# 주석 블록 종류.
NoteBlockKind = Literal["paragraph", "table", "subheading"]

# 표 셀 값: 숫자 파싱 성공 시 float, 빈 값이면 None, 파싱 불가면 원문 str.
CellValue = Union[str, float, None]


@dataclass
class DocNode:
    """main.do 의 viewDoc(...) 호출 하나에 대응하는 문서 트리 노드.

    Attributes:
        title: 노드 제목 원문 (예: "4. 재무제표"). 글자 사이 공백이 있을 수 있다.
        rcp_no: 접수번호 14자리.
        dcm_no: 문서번호.
        ele_id: 요소 ID.
        offset: 본문 내 시작 오프셋.
        length: 본문 길이.
        dtd: DTD 파일명 (보통 "dart3.xsd").
        url: viewer.do 완성 URL.
        depth: 트리 깊이 (0 = 최상위).
        parent_title: 상위 노드 제목. 최상위면 None.
        children: 직계 자식 노드 (문서 순서). 트리 관계 보존용.
    """

    title: str
    rcp_no: str
    dcm_no: str
    ele_id: str
    offset: str
    length: str
    dtd: str
    url: str
    depth: int = 0
    parent_title: Optional[str] = None
    children: list["DocNode"] = field(default_factory=list, repr=False)


@dataclass
class Table:
    """colspan/rowspan 을 전개한 2차원 표.

    Attributes:
        caption: 표 바로 위 캡션/제목 문구. 없으면 None.
        unit: 단위 문구 원문 (예: "(단위 : 원)"). 없으면 None.
        header_rows: 헤더 행들. 2단 헤더면 길이 2. 모든 셀은 str.
        rows: 본문 행들. 금액은 float, 빈 값은 None, 파싱 불가는 str.
        depths: 본문 행별 계정과목(첫 열) 들여쓰기 깊이. ``rows`` 와 길이가 같다.
        note_refs: 본문 행별 주석참조(``현금및현금성자산 (주4,29)`` → ``"4,29"``). 없으면 None. ``rows`` 와 길이가 같다.
    """

    caption: Optional[str] = None
    unit: Optional[str] = None
    header_rows: list[list[str]] = field(default_factory=list)
    rows: list[list[CellValue]] = field(default_factory=list)
    depths: list[int] = field(default_factory=list)
    note_refs: list[Optional[str]] = field(default_factory=list)


@dataclass
class Statement:
    """재무제표 한 개 (= Excel 시트 한 개).

    Attributes:
        kind: 재무제표 종류.
        scope: 연결/별도/단일.
        title: 표 제목 원문 (공백 제거 전).
        period_text: 기간 문구 원문 (예: "제 55 기 2023.01.01 부터 2023.12.31 까지").
        unit: 단위 문구 원문.
        table: 표 데이터.
        suffix: 같은 kind 가 여러 번 나올 때 두 번째부터 "_2", "_3". 기본 "".
        basis: 종류 판정 근거 ("제목" | "내용").
    """

    kind: StatementKind
    scope: Scope
    title: str
    period_text: Optional[str]
    unit: Optional[str]
    table: Table
    suffix: str = ""
    basis: str = "제목"


@dataclass
class NoteBlock:
    """주석 내부의 순서가 있는 블록 하나.

    Attributes:
        kind: "paragraph" | "table" | "subheading".
        text: paragraph/subheading 일 때 본문. table 이면 None.
        table: table 일 때 표. 그 외 None.
    """

    kind: NoteBlockKind
    text: Optional[str] = None
    table: Optional[Table] = None


@dataclass
class Note:
    """주석 번호 하나 (= Excel 시트 한 개).

    Attributes:
        number: 주석 번호 (예: 21). 하위 번호(2.1, (1), 가.)는 별도 Note 가 아니다.
        title: 주석 제목 (예: "우발부채와 약정사항").
        scope: 연결/별도/단일.
        blocks: 원문 순서대로의 블록 목록.
        source: 제목 출처 ("structure" 구조 신호 | "text" 텍스트 정규식 | "expected" 트리 제목으로 대체
            | "inferred" 제목 없이 선두 블록을 배정).
    """

    number: int
    title: str
    scope: Scope
    blocks: list[NoteBlock] = field(default_factory=list)
    source: str = "text"


@dataclass
class ParsedReport:
    """파싱 결과 전체.

    Attributes:
        meta: rcp_no, company, report_name, base_date, parsed_at, unit, source_url 등.
        statements: 재무제표 목록.
        notes: 주석 목록.
        warnings: 파싱 중 누적된 경고. 예외로 죽이지 않고 여기에 쌓는다.
    """

    meta: dict = field(default_factory=dict)
    statements: list[Statement] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
