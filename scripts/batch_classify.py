"""배치 검증용 경고·실패 자동 분류.

경고 문자열과 결과 지표를 코드로 매핑한다. 매핑되지 않는 경고는 ``W_OTHER`` (원문 보존).
"정상" = 성공 + F_*/N_*/E_* 코드 없음 (W_* 만 허용).
"""

from __future__ import annotations

# 코드 → 설명 (summary.md 용)
CODE_DESCRIPTIONS: dict[str, str] = {
    "E_EXC": "예외 발생 (변환 실패 또는 부분 수신/파싱 실패)",
    "E_NO_CONTENT": "문서에 재무 내용 없음 (정정·첨부·연장 공시, 표 없는 첨부 — 파서 문제 아님)",
    "E_NET": "네트워크 오류 (연결 끊김/타임아웃 — IP 차단 추정 신호)",
    "E_TIMEOUT": "60초 타임아웃",
    "F_NO_BS": "어느 스코프든 재무상태표 없음",
    "F_NO_STMT": "재무제표 0개",
    "F_KIND_MISSING": "재무상태표·손익(또는 포괄손익)·현금흐름표 중 하나라도 없음",
    "F_BALANCE": "대차 불일치 경고",
    "F_BALANCE_NA": "대차 검증 불가",
    "F_STMT_DUP": "같은 kind 중복 (_2 suffix)",
    "F_STMT_CONTENT": "내용 기반 분류 사용",
    "N_NONE": "주석 0개",
    "N_UNSORTED": "주석00_미분류",
    "N_INFERRED": "inferred 주석 (1번 제목 미확인)",
    "N_GAP": "주석 번호 누락",
    "N_FEW": "주석 5개 미만",
    "N_MISMATCH": "expected_titles 불일치 (누락/초과/제목 불일치/자식 개별 수신)",
    "N_TRUNC": "주석 제목 잘림 의심",
    "W_MIXED": "숫자 열 문자열 혼입 집계 (정상, 통계만)",
    "W_MIXED_STMT": "재무제표 표 문자열 혼입 집계 (정상, 통계만)",
    "W_BRANCH_PARENT": "주석 N 과 N-M 공존 — N 을 상위로 유지 (정상, 안내)",
    "W_MERGED": "페이지 분할 표 병합 안내 (정상)",
    "W_PAD": "열 수 불일치 패딩",
    "W_RELATED": "정정본 안내",
    "W_OTHER": "미분류 경고 (원문 보존)",
}

# (부분 문자열, 코드) — 위에서부터 첫 매칭. 순서 중요.
_WARNING_RULES: list[tuple[str, str]] = [
    ("파싱 오류 가능", "F_BALANCE"),
    ("대차 검증 불가", "F_BALANCE_NA"),
    ("suffix", "F_STMT_DUP"),
    ("내용 기반 분류", "F_STMT_CONTENT"),
    ("주석00_미분류", "N_UNSORTED"),
    ("주석 번호 형식이 없어", "N_UNSORTED"),
    ("제목을 찾지 못해", "N_INFERRED"),
    ("잘렸을 수 있음", "N_TRUNC"),
    ("개별 수신", "N_MISMATCH"),
    ("초과 검출", "N_MISMATCH"),
    ("제목 불일치", "N_MISMATCH"),
    ("누락(기대 제목", "N_MISMATCH"),
    ("본문에서 찾지 못함", "N_MISMATCH"),
    ("트리 순서와 어긋남", "N_MISMATCH"),
    ("직접 검출 실패", "N_MISMATCH"),
    ("누락 (검출 순서상", "N_GAP"),
    ("재무제표 표", "W_MIXED_STMT"),  # "재무제표 표 N개에서 숫자 열에 …" — W_MIXED 보다 먼저
    ("숫자 열에 문자열 값이 섞여", "W_MIXED"),
    ("숫자로 읽지 못한 값", "W_MIXED_STMT"),  # 집계 전 표별 원문 형태 (구버전 결과 호환)
    ("이 함께 있습니다", "W_BRANCH_PARENT"),
    ("헤더가 같은 표", "W_MERGED"),
    ("열 수 불일치", "W_PAD"),
    ("정정본이 있습니다", "W_RELATED"),
    ("재무제표 섹션이 없습니다", "E_NO_CONTENT"),
    ("정정신고 공시로 보이며", "E_NO_CONTENT"),
    ("본문에 표가 없습니다", "E_NO_CONTENT"),
    ("연장 신고서로 보이며", "E_NO_CONTENT"),
    ("RemoteDisconnected", "E_NET"),
    ("ConnectionError", "E_NET"),
    ("ConnectTimeout", "E_NET"),
    ("ReadTimeout", "E_NET"),
    ("Max retries", "E_NET"),
    ("수신/파싱 실패", "E_EXC"),
    ("요청을 거부했습니다", "E_EXC"),
]


def classify_warning(warning: str) -> str:
    """경고 문자열 하나를 코드로 매핑한다. 매핑되지 않으면 ``W_OTHER``.

    Args:
        warning: ParsedReport.warnings 의 항목.

    Returns:
        분류 코드.
    """
    for needle, code in _WARNING_RULES:
        if needle in warning:
            return code
    return "W_OTHER"


def derive_codes(
    kinds_by_scope: dict[str, set[str]],
    notes_by_scope: dict[str, list[int]],
    statement_count: int,
) -> list[str]:
    """결과 지표에서 파생 코드를 계산한다 (경고 문자열과 무관한 구조적 실패).

    Args:
        kinds_by_scope: 스코프 → 재무제표 kind 집합.
        notes_by_scope: 스코프 → 주석 번호 목록.
        statement_count: 재무제표 수.

    Returns:
        파생 코드 목록 (중복 없음, 정렬).
    """
    codes: set[str] = set()
    if statement_count == 0:
        codes.add("F_NO_STMT")
    for kinds in kinds_by_scope.values():
        if "재무상태표" not in kinds:
            codes.add("F_NO_BS")
        has_pl = "손익계산서" in kinds or "포괄손익계산서" in kinds
        if not ("재무상태표" in kinds and has_pl and "현금흐름표" in kinds):
            codes.add("F_KIND_MISSING")
    if not notes_by_scope or all(not nums for nums in notes_by_scope.values()):
        codes.add("N_NONE")
    for nums in notes_by_scope.values():
        if 0 in nums:
            codes.add("N_UNSORTED")
        if 0 < len(nums) < 5:
            codes.add("N_FEW")
    return sorted(codes)


def is_normal(success: bool, codes: list[str]) -> bool:
    """정상 여부: 성공이면서 F_*/N_*/E_* 코드가 없다 (W_* 만 허용).

    Args:
        success: convert_report 성공 여부.
        codes: 해당 건의 전체 코드 목록.

    Returns:
        정상이면 True.
    """
    return success and not any(c.startswith(("F_", "N_", "E_")) for c in codes)
