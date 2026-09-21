"""main.do → 문서 트리 추출, 노드 URL 생성, 입력 URL 해석, 대상 노드 선택.

네트워크 호출 없음. HTML 문자열만 받는다.

실제 main.do 의 ``function makeToc()`` 안에는 jstree 데이터가 다음 형태로 들어 있다::

    var node1 = {};
    node1['text'] = "III. 재무에 관한 사항";
    node1['id'] = "17";
    node1['rcpNo'] = "20260310002820";
    node1['dcmNo'] = "11104488";
    node1['eleId'] = "17";
    node1['offset'] = "...";
    node1['length'] = "...";
    node1['dtd'] = "dart4.xsd";
    node1['tocNo'] = "17";
    node1['children'] = [];
        var node2 = {};
        node2['text'] = "2. 연결재무제표";
        ...
        node1['children'].push(node2);
    treeData.push(node1);

변수 이름 ``nodeN`` 의 N 이 곧 트리 깊이(node1 = depth 0)이다.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from dart.models import DocNode

VIEWER_URL = "https://dart.fss.or.kr/report/viewer.do"

RCP_NO_RE = re.compile(r"(?<!\d)(\d{14})(?!\d)")

# var node2 = {};
_NODE_START_RE = r"var\s+node(?P<depth>\d+)\s*=\s*\{\s*\}"
# node2['text'] = "...";   (값 안의 \" 이스케이프 허용)
_NODE_PROP_RE = r"node(?P<pdepth>\d+)\[\s*['\"](?P<key>\w+)['\"]\s*\]\s*=\s*\"(?P<val>(?:[^\"\\]|\\.)*)\""
_TREE_TOKEN_RE = re.compile(f"(?:{_NODE_START_RE})|(?:{_NODE_PROP_RE})")

_WS_RE = re.compile(r"[\s　 ]+")
# 정규화 후 앞의 번호 접두: "4", "III", "Ⅲ", "iv", "2-1" 등 (점은 이미 제거됨)
_NUM_PREFIX_RE = re.compile(r"^[0-9IVXivxⅠ-Ⅻⅰ-ⅻ\-]+")

_FIN_SECTION = "재무에관한사항"  # 정기보고서에서 재무제표 후보의 부모 절
_ATTACH_PREFIX = "(첨부)"  # 감사보고서 첨부 노드 접두

KEY_CONSOL_FS = "연결_재무제표"
KEY_CONSOL_NOTES = "연결_주석"
KEY_SEP_FS = "별도_재무제표"
KEY_SEP_NOTES = "별도_주석"
KEY_SINGLE_FS = "재무제표"
KEY_SINGLE_NOTES = "주석"


def parse_report_input(user_input: str) -> str:
    """사용자 입력에서 rcpNo(14자리)를 뽑아낸다.

    main.do URL, viewer.do URL, 14자리 숫자 단독, 앞뒤 공백·따옴표가 붙은 입력을 모두 허용한다.
    URL 이면 쿼리의 ``rcpNo`` 를 우선 사용하고, 없으면 문자열 전체에서 14자리 숫자를 찾는다.

    Args:
        user_input: 사용자 입력 문자열.

    Returns:
        rcpNo 14자리 문자열.

    Raises:
        ValueError: rcpNo 를 찾을 수 없는 경우.
    """
    text = (user_input or "").strip().strip("\"'`<>()[] \t\r\n")
    if "?" in text:
        qs = parse_qs(urlparse(text).query)
        for v in qs.get("rcpNo", []):
            m = RCP_NO_RE.search(v)
            if m:
                return m.group(1)
    m = RCP_NO_RE.search(text)
    if m:
        return m.group(1)
    raise ValueError(f"rcpNo를 찾을 수 없습니다: {user_input!r}")


def parse_dcm_no(user_input: str) -> str | None:
    """사용자 입력 URL 에서 ``dcmNo`` 쿼리 파라미터를 뽑는다. 없으면 None.

    사업보고서에 첨부된 감사보고서는 ``main.do?rcpNo=..&dcmNo=..`` 형태로 지정된다
    (실측 20260310002820: dcmNo 11104486 별도 감사보고서, 11104487 연결 감사보고서).
    main.do 에 dcmNo 를 함께 주면 그 첨부 문서의 트리가 온다.

    Args:
        user_input: 사용자 입력 문자열.

    Returns:
        dcmNo 숫자 문자열 또는 None.
    """
    text = (user_input or "").strip().strip("\"'`<>()[] \t\r\n")
    if "?" not in text:
        return None
    qs = parse_qs(urlparse(text).query)
    for v in qs.get("dcmNo", []):
        m = re.search(r"(\d{4,})", v)
        if m:
            return m.group(1)
    return None


def build_viewer_url(node: DocNode) -> str:
    """노드의 viewDoc 파라미터로 viewer.do 본문 URL 을 조립한다.

    Args:
        node: 문서 트리 노드.

    Returns:
        ``https://dart.fss.or.kr/report/viewer.do?rcpNo=..&dcmNo=..&eleId=..&offset=..&length=..&dtd=..``
    """
    return (
        f"{VIEWER_URL}?rcpNo={node.rcp_no}&dcmNo={node.dcm_no}&eleId={node.ele_id}"
        f"&offset={node.offset}&length={node.length}&dtd={node.dtd}"
    )


def _unescape_js(s: str) -> str:
    """JS 문자열 리터럴의 간단한 이스케이프(\\" \\\\ \\/)를 푼다."""
    return s.replace('\\"', '"').replace("\\/", "/").replace("\\\\", "\\")


def _toc_script_region(main_html: str) -> str:
    """``function makeToc()`` 부터 jstree 초기화까지의 구간을 잘라낸다. 없으면 전체."""
    start = main_html.find("function makeToc")
    if start < 0:
        return main_html
    end = main_html.find(".jstree(", start)
    return main_html[start:end] if end > start else main_html[start:]


def parse_doc_tree(main_html: str, rcp_no: str) -> list[DocNode]:
    """main.do HTML 의 makeToc 스크립트에서 ``nodeN[...]`` 대입문을 읽어 문서 트리를 만든다.

    ``var nodeN = {}`` 가 나오면 depth = N-1 인 새 노드를 시작하고, 이어지는
    ``nodeN['key'] = "value"`` 를 그 노드의 속성으로 모은다. parent_title 은
    직전에 나온 depth-1 노드의 제목이다. ``rcpNo`` 속성이 없으면 인자 ``rcp_no`` 를 쓴다.

    Args:
        main_html: main.do 응답 HTML.
        rcp_no: 접수번호 (노드에 rcpNo 가 없을 때 대체값).

    Returns:
        문서 순서대로의 :class:`DocNode` 목록. 찾지 못하면 빈 리스트.
    """
    region = _toc_script_region(main_html)
    raw_nodes: list[tuple[int, dict[str, str]]] = []
    current: dict[str, str] | None = None
    current_depth = 0

    for m in _TREE_TOKEN_RE.finditer(region):
        if m.group("depth") is not None:
            current_depth = int(m.group("depth")) - 1
            current = {}
            raw_nodes.append((current_depth, current))
        elif current is not None and int(m.group("pdepth")) - 1 == current_depth:
            current[m.group("key")] = _unescape_js(m.group("val"))

    nodes: list[DocNode] = []
    last_at_depth: dict[int, DocNode] = {}
    for depth, props in raw_nodes:
        if "eleId" not in props:
            continue
        parent = last_at_depth.get(depth - 1) if depth > 0 else None
        node = DocNode(
            title=props.get("text", ""),
            rcp_no=props.get("rcpNo") or rcp_no,
            dcm_no=props.get("dcmNo", ""),
            ele_id=props.get("eleId", ""),
            offset=props.get("offset", ""),
            length=props.get("length", ""),
            dtd=props.get("dtd", ""),
            url="",
            depth=depth,
            parent_title=parent.title if parent else None,
        )
        node.url = build_viewer_url(node)
        nodes.append(node)
        if parent is not None:
            parent.children.append(node)
        last_at_depth[depth] = node
        for d in [d for d in last_at_depth if d > depth]:
            del last_at_depth[d]
    return nodes


def normalize_title(title: str) -> str:
    """비교용 제목 정규화.

    모든 공백(전각·NBSP 포함)과 ``.`` 을 제거하고, 앞의 번호 접두("4", "III", "Ⅲ", "2-1")를 뗀다.
    소문자화는 하지 않는다 (한글).

    Args:
        title: 제목 원문 (예: "4. 재 무 제 표", "III. 재무에 관한 사항").

    Returns:
        정규화된 제목 (예: "재무제표", "재무에관한사항").
    """
    s = _WS_RE.sub("", title or "").replace(".", "")
    return _NUM_PREFIX_RE.sub("", s)


def _classify(node: DocNode) -> str | None:
    """노드를 후보 키로 분류한다. 별도/단일 구분 전이므로 임시 키 ``_별도_*`` 를 쓴다.

    후보 자격(오매칭 방지, 실측 20260319000808): '재무에 관한 사항' 절의 직계 자식,
    최상위 노드, ``(첨부)`` 로 시작하는 노드, 그리고 재무제표 노드의 자식 ``주석`` 만 후보다.
    주석 노드의 자식(예: '2. 재무제표 작성기준 및 중요한 회계정책')은 절대 후보가 아니다.
    제목은 정규화 후 **정확히** 재무제표/연결재무제표/재무제표주석/연결재무제표주석 이어야 한다
    (번호 접두는 :func:`normalize_title` 이 제거하므로 "4. 재무제표" 는 매칭, 부분 문자열 포함은 불가).
    """
    t = normalize_title(node.title)
    if not t:
        return None
    parent = normalize_title(node.parent_title or "")
    if t == "주석":
        pcore = parent[len(_ATTACH_PREFIX):] if parent.startswith(_ATTACH_PREFIX) else parent
        if node.depth == 0 or pcore in ("재무제표", "연결재무제표"):
            return KEY_CONSOL_NOTES if "연결" in pcore else "_별도_주석"
        return None
    core = t[len(_ATTACH_PREFIX):] if t.startswith(_ATTACH_PREFIX) else t
    if not (_FIN_SECTION in parent or node.depth == 0 or t.startswith(_ATTACH_PREFIX)):
        return None
    if core == "연결재무제표주석":
        return KEY_CONSOL_NOTES
    if core == "연결재무제표":
        return KEY_CONSOL_FS
    if core == "재무제표주석":
        return "_별도_주석"
    if core == "재무제표":
        return "_별도_재무제표"
    return None


def select_target_nodes(nodes: list[DocNode], warnings: list[str] | None = None) -> dict[str, DocNode]:
    """재무제표/주석 노드를 골라낸다.

    키 규칙:
        - 연결 노드가 있으면 ``연결_재무제표``, ``연결_주석``; 별도 노드는 ``별도_재무제표``, ``별도_주석``.
        - 연결 노드가 하나도 없으면(감사보고서 등) ``재무제표``, ``주석``.
        - 연결감사보고서(연결만 있음)는 ``연결_*`` 만 채운다.

    매칭 규칙은 :func:`_classify` 참조 (정확 일치 + 후보 자격 제한).
    제목이 그냥 ``주석`` 이면 상위 제목에 ``연결`` 이 있는지로 스코프를 정한다.
    같은 키에 후보가 둘 이상이면 문서 순서상 첫 번째를 택하고 ``warnings`` 에 기록한다.

    Args:
        nodes: :func:`parse_doc_tree` 결과.
        warnings: 경고 누적 리스트. None 이면 기록하지 않는다.

    Returns:
        키 → :class:`DocNode`. 해당 노드가 없는 키는 포함하지 않는다.
    """
    candidates: dict[str, list[DocNode]] = {}
    for node in nodes:
        key = _classify(node)
        if key:
            candidates.setdefault(key, []).append(node)

    has_consol = KEY_CONSOL_FS in candidates or KEY_CONSOL_NOTES in candidates
    key_map = {
        KEY_CONSOL_FS: KEY_CONSOL_FS,
        KEY_CONSOL_NOTES: KEY_CONSOL_NOTES,
        "_별도_재무제표": KEY_SEP_FS if has_consol else KEY_SINGLE_FS,
        "_별도_주석": KEY_SEP_NOTES if has_consol else KEY_SINGLE_NOTES,
    }

    result: dict[str, DocNode] = {}
    for tmp_key, final_key in key_map.items():
        cands = candidates.get(tmp_key)
        if not cands:
            continue
        chosen = cands[0]  # 문서 순서상 첫 번째
        if len(cands) > 1 and warnings is not None:
            titles = ", ".join(f"{c.title!r}(depth={c.depth}, eleId={c.ele_id})" for c in cands)
            warnings.append(f"[{final_key}] 후보 {len(cands)}개 중 문서 순서상 첫 번째 {chosen.title!r} 선택: {titles}")
        result[final_key] = chosen
    return result


def describe_tree(nodes: list[DocNode]) -> str:
    """디버그용. depth 들여쓰기로 제목·eleId·length 를 한 줄씩 출력한다.

    Args:
        nodes: 문서 트리.

    Returns:
        여러 줄 문자열.
    """
    return "\n".join(f"{'  ' * n.depth}{n.title}  [eleId={n.ele_id}, length={n.length}]" for n in nodes)
