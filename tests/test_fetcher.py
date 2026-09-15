"""dart.fetcher 테스트. 가짜 Response 로 디코딩/차단 판정을 검사하고, 실제 네트워크는 network 마커."""

from __future__ import annotations

from unittest import mock

import pytest
import requests

from dart import fetcher
from dart.fetcher import DartBlockedError, build_session, decode_response, fetch_html, fetch_main


def _fake_response(content: bytes, encoding: str | None = None, status: int = 200, url: str = "http://x") -> requests.Response:
    resp = requests.Response()
    resp._content = content  # noqa: SLF001
    resp.status_code = status
    resp.encoding = encoding
    resp.url = url
    return resp


def test_build_session_headers() -> None:
    s = build_session()
    assert "Chrome/" in s.headers["User-Agent"]
    assert s.headers["Referer"] == "https://dart.fss.or.kr/dsaf001/main.do"
    assert s.headers["Accept-Language"].startswith("ko-KR")


def test_decode_response_utf8_meta() -> None:
    body = '<html><head><meta charset="UTF-8"></head><body>재무상태표</body></html>'.encode("utf-8")
    assert "재무상태표" in decode_response(_fake_response(body, encoding="ISO-8859-1"))


def test_decode_response_euckr_meta() -> None:
    body = '<html><head><meta http-equiv="Content-Type" content="text/html; charset=euc-kr"></head><body>재무상태표</body></html>'.encode("cp949")
    assert "재무상태표" in decode_response(_fake_response(body, encoding=None))


def test_decode_response_euckr_without_meta_falls_back() -> None:
    """meta 없음 + resp.encoding 오답(utf-8) → 대체문자 비율 초과 → cp949 로 넘어간다."""
    body = ("<html><body>" + "재무상태표 손익계산서 " * 50 + "</body></html>").encode("cp949")
    text = decode_response(_fake_response(body, encoding="utf-8"))
    assert "재무상태표" in text and "�" not in text


def test_decode_response_garbage_does_not_raise() -> None:
    body = bytes(range(128, 256)) * 20
    text = decode_response(_fake_response(body, encoding=None))
    assert isinstance(text, str) and len(text) > 0


@pytest.mark.parametrize(
    "body",
    [
        "<html>짧음</html>",
        "<html>" + "x" * 600 + "접근이 거부되었습니다</html>",
        "<html>" + "x" * 600 + "비정상적인 접근입니다</html>",
    ],
)
def test_fetch_html_blocked(monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    monkeypatch.setattr(fetcher.time, "sleep", lambda _s: None)
    session = mock.Mock()
    session.get.return_value = _fake_response(body.encode("utf-8"))
    with pytest.raises(DartBlockedError) as ei:
        fetch_html(session, "http://x")
    assert session.get.call_count == len(fetcher.RETRY_DELAYS_SEC) + 1
    assert len(ei.value.snippet) <= 200


def test_fetch_html_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(fetcher.time, "sleep", lambda s: sleeps.append(s))
    ok = _fake_response(("<html>" + "정상 본문 " * 200 + "</html>").encode("utf-8"))
    session = mock.Mock()
    session.get.side_effect = [requests.ConnectionError("boom"), ok]
    text = fetch_html(session, "http://x")
    assert "정상 본문" in text
    assert session.get.call_count == 2
    assert 0.5 in sleeps  # 첫 재시도 대기


def test_fetch_main_url(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def fake_fetch(session: object, url: str) -> str:
        seen["url"] = url
        return "<html/>"

    monkeypatch.setattr(fetcher, "fetch_html", fake_fetch)
    fetch_main(mock.Mock(), "20260310002820")
    assert seen["url"] == "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260310002820"


@pytest.mark.network
def test_fetch_main_real() -> None:
    html = fetch_main(build_session(), "20260414001300")
    assert "function makeToc" in html and "node1['eleId']" in html
