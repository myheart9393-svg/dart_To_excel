"""scripts.batch_classify 의 경고 매핑·파생 코드·정상 판정 테스트 (네트워크 없음)."""

from __future__ import annotations

import pytest

from scripts.batch_classify import CODE_DESCRIPTIONS, classify_warning, derive_codes, is_normal


@pytest.mark.parametrize(
    "warning, code",
    [
        ("재무상태표(연결) 제 57 기: 자산총계 1 ≠ 부채 2 + 자본 3 (차이 4) — 파싱 오류 가능", "F_BALANCE"),
        ("재무상태표(단일) 대차 검증 불가: 부채총계 행 없음", "F_BALANCE_NA"),
        ("손익계산서: 같은 종류의 표가 다시 나와 suffix '_2' 부여 (표 순번 3)", "F_STMT_DUP"),
        ("제목 없이 내용 기반 분류: 재무상태표, 표 순번 1", "F_STMT_CONTENT"),
        ("첫 주석 번호가 1 이 아님(3) → 주석00_미분류 하나로 반환", "N_UNSORTED"),
        ("주석 1 제목을 찾지 못해 2번 이전 블록을 주석 1로 배정함 — 시트 주석01 확인 필요", "N_INFERRED"),
        ("주석 13 누락 (검출 순서상 12 → 14)", "N_GAP"),
        ("주석 4 누락(기대 제목: 차입금)", "N_MISMATCH"),
        ("주석 35 초과 검출(제목: 가짜)", "N_MISMATCH"),
        ("주석 2 제목 불일치: 검출 'a' / 기대 'b'", "N_MISMATCH"),
        ("주석 분할 불일치로 자식 노드 34개 개별 수신 (연결)", "N_MISMATCH"),
        ("주석 22 제목이 잘렸을 수 있음 (bookmarktext 20자, 내부 텍스트 없음): '...'", "N_TRUNC"),
        ("주석 표 11개에서 숫자 열에 문자열 값이 섞여 원문 그대로 남김 (주석 12)", "W_MIXED"),
        ("재무제표 표 2개에서 숫자 열에 문자열 값이 섞여 원문 그대로 남김 (재무상태표, 자본변동표)", "W_MIXED_STMT"),
        ("[재 무 상 태 표] 숫자 열 '제 21 (당) 기' 에 숫자로 읽지 못한 값 2개가 문자열로 남았습니다.", "W_MIXED_STMT"),
        ("주석 14 과 14-1 이 함께 있습니다 — 14 를 상위로 유지", "W_BRANCH_PARENT"),
        ("재무상태표: 헤더가 같은 표 7를 병합", "W_MERGED"),
        ("표 3: 열 수 불일치(최대 4, 최소 1)로 패딩", "W_PAD"),
        ("이 공시에 정정본이 있습니다: 2026.05.01 [기재정정] (rcpNo=1)", "W_RELATED"),
        ("이 공시에는 재무제표 섹션이 없습니다(정정·첨부 공시일 수 있음). 같은 공시의 관련 문서: …", "E_NO_CONTENT"),
        ("정정신고 공시로 보이며 재무제표 섹션이 없습니다. 원 공시의 rcpNo를 입력하세요.", "E_NO_CONTENT"),
        ("재무제표 노드는 있으나 본문에 표가 없습니다(이미지 첨부 또는 기재 생략으로 추정). …", "E_NO_CONTENT"),
        ("제출기한 연장 신고서로 보이며 재무제표가 없습니다. 원 공시 제출 후 그 rcpNo를 입력하세요.", "E_NO_CONTENT"),
        ("[연결_주석] 수신/파싱 실패: RuntimeError: boom", "E_EXC"),
        ("[연결_주석] 수신/파싱 실패: ConnectionError: ('Connection aborted.', RemoteDisconnected(...))", "E_NET"),
        ("[재무제표] 수신/파싱 실패: ReadTimeout: HTTPSConnectionPool", "E_NET"),
        ("듣도 보도 못한 경고", "W_OTHER"),
    ],
)
def test_classify_warning(warning: str, code: str) -> None:
    assert classify_warning(warning) == code
    assert code in CODE_DESCRIPTIONS


def test_derive_codes() -> None:
    ok = derive_codes({"연결": {"재무상태표", "손익계산서", "현금흐름표"}}, {"연결": list(range(1, 31))}, 3)
    assert ok == []
    assert derive_codes({}, {}, 0) == ["F_NO_STMT", "N_NONE"]  # 스코프 자체가 없으면 per-scope 코드는 없음
    assert "F_NO_STMT" in derive_codes({}, {"연결": [1, 2, 3, 4, 5]}, 0)
    assert "F_NO_BS" in derive_codes({"연결": {"손익계산서", "현금흐름표"}}, {"연결": [1, 2, 3, 4, 5]}, 2)
    assert "F_KIND_MISSING" in derive_codes({"연결": {"재무상태표", "손익계산서"}}, {"연결": [1, 2, 3, 4, 5]}, 2)
    # 포괄손익계산서만 있어도 손익 조건 충족
    assert "F_KIND_MISSING" not in derive_codes(
        {"연결": {"재무상태표", "포괄손익계산서", "현금흐름표"}}, {"연결": [1, 2, 3, 4, 5]}, 3
    )
    assert "N_NONE" in derive_codes({"연결": {"재무상태표", "손익계산서", "현금흐름표"}}, {}, 3)
    assert "N_FEW" in derive_codes({"연결": {"재무상태표", "손익계산서", "현금흐름표"}}, {"연결": [1, 2, 3]}, 3)
    assert "N_UNSORTED" in derive_codes({"연결": {"재무상태표", "손익계산서", "현금흐름표"}}, {"연결": [0]}, 3)


def test_is_normal() -> None:
    assert is_normal(True, ["W_MIXED", "W_RELATED"])
    assert is_normal(True, ["W_MIXED_STMT", "W_BRANCH_PARENT", "W_MERGED"])
    assert not is_normal(True, ["W_MIXED", "N_GAP"])
    assert not is_normal(True, ["F_BALANCE"])
    assert not is_normal(False, [])
    assert not is_normal(True, ["E_TIMEOUT"])
    assert not is_normal(True, ["E_NET"])
    assert not is_normal(False, ["E_NO_CONTENT"])
