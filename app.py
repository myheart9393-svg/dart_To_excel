"""Streamlit UI. 파싱 로직은 두지 않고 dart.pipeline.convert_report 만 호출한다."""

from __future__ import annotations

import re
import traceback

import streamlit as st

from dart.pipeline import ConversionError, convert_report

st.set_page_config(page_title="DART → Excel", page_icon="📊", layout="centered")

st.title("DART → Excel 변환기")
st.caption(
    "DART 보고서 URL 또는 14자리 접수번호(rcpNo)를 입력하면 "
    "재무제표 4종과 주석을 시트별로 나눈 .xlsx 를 만듭니다."
)

with st.form("convert_form"):
    user_input = st.text_input(
        "DART 보고서 URL 또는 rcpNo",
        placeholder="https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20240312000736  또는 rcpNo 14자리",
    )
    split_notes_opt = st.checkbox("주석을 주석번호별 시트로 분리", value=True)
    submitted = st.form_submit_button("변환 시작", type="primary")  # 입력창에서 Enter 도 제출

if "result" not in st.session_state:
    st.session_state.result = None
    st.session_state.result_input = None

# 새 URL 을 입력하면 이전 결과를 초기화한다
if st.session_state.result is not None and st.session_state.result_input != user_input.strip():
    st.session_state.result = None


def _safe_filename(*parts: str) -> str:
    """파일명 금지문자를 제거하고 빈 조각은 생략해 '_' 로 잇는다."""
    cleaned = [re.sub(r'[\\/:*?"<>|]+', "", p).strip() for p in parts if p]
    return "_".join(c for c in cleaned if c) or "report"


if submitted and not user_input.strip():
    st.warning("URL 또는 rcpNo 를 입력하세요.")
elif submitted:
    st.session_state.result = None
    progress_bar = st.progress(0.0, text="준비 중...")

    def _on_progress(ratio: float, message: str) -> None:
        progress_bar.progress(min(max(ratio, 0.0), 1.0), text=message)

    try:
        report, xlsx_bytes = convert_report(user_input.strip(), progress=_on_progress, split_note_sheets=split_notes_opt)
        st.session_state.result = (report, xlsx_bytes)
        st.session_state.result_input = user_input.strip()
    except ConversionError as exc:
        progress_bar.empty()
        st.error(str(exc))
        with st.expander("상세 오류"):
            st.code(traceback.format_exc())
    except Exception as exc:  # noqa: BLE001 - UI 에서는 모든 예외를 사용자에게 보여준다
        progress_bar.empty()
        st.error(f"변환 실패: {type(exc).__name__}: {exc}")
        with st.expander("상세 오류"):
            st.code(traceback.format_exc())

if st.session_state.result is not None:
    report, xlsx_bytes = st.session_state.result
    meta = report.meta
    file_name = _safe_filename(meta.get("company", ""), meta.get("report_name", ""), meta.get("rcp_no", "")) + ".xlsx"

    st.success(f"재무제표 {len(report.statements)}개, 주석 {len(report.notes)}개를 추출했습니다.")
    st.download_button(
        "Excel 다운로드",
        data=xlsx_bytes,
        file_name=file_name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    with st.expander("결과 요약", expanded=True):
        st.markdown(f"**파일명**: `{file_name}`")
        if report.statements:
            st.markdown("**재무제표**")
            st.table(
                [
                    {"스코프": s.scope, "종류": s.kind + s.suffix, "행 수": len(s.table.rows), "단위": s.unit or ""}
                    for s in report.statements
                ]
            )
        if report.notes:
            by_scope: dict[str, list[int]] = {}
            for n in report.notes:
                by_scope.setdefault(n.scope, []).append(n.number)
            lines = [f"- {scope}: {len(nums)}개 (번호 {min(nums)}~{max(nums)})" for scope, nums in by_scope.items()]
            st.markdown("**주석**\n" + "\n".join(lines))
        if report.warnings:
            st.warning(f"경고 {len(report.warnings)}건")
            for w in report.warnings:
                st.markdown(f"- {w}")
        else:
            st.markdown("경고 없음")
