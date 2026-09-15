"""HTTP 세션, 헤더, 인코딩 처리, 재시도.

이 프로젝트에서 네트워크 호출은 **이 모듈에만** 존재한다.
Playwright 는 기본 경로에서 사용하지 않는다 (requests 실패 시 선택적 fallback 만 허용).
"""

from __future__ import annotations

import logging
import re
import time

import requests

logger = logging.getLogger(__name__)

MAIN_URL = "https://dart.fss.or.kr/dsaf001/main.do"
VIEWER_URL = "https://dart.fss.or.kr/report/viewer.do"

DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Referer": MAIN_URL,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

TIMEOUT_SEC = 15.0
RETRY_DELAYS_SEC: tuple[float, ...] = (0.5, 1.0, 2.0)
MIN_INTERVAL_SEC = 0.3
MIN_BODY_LEN = 500
BLOCKED_PHRASES: tuple[str, ...] = ("접근이 거부", "잘못된 접근", "비정상적인 접근")
REPLACEMENT_RATIO_MAX = 0.05

_META_CHARSET_RE = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([A-Za-z0-9_\-]+)", re.I)

_last_request_at: float = 0.0


class DartBlockedError(RuntimeError):
    """DART 가 200 을 돌려줬지만 본문이 차단/오류 페이지인 경우."""

    def __init__(self, url: str, snippet: str) -> None:
        self.url = url
        self.snippet = snippet
        super().__init__(f"DART 응답이 차단/오류 페이지입니다: {url}\n{snippet}")


def build_session() -> requests.Session:
    """브라우저 User-Agent, Referer(main.do), Accept-Language 가 설정된 세션을 만든다.

    Returns:
        DART 접근용 :class:`requests.Session`.
    """
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    return session


def _normalize_encoding(name: str) -> str:
    """인코딩 이름을 Python codec 이름으로 정리한다 (euc-kr → cp949 등)."""
    n = name.strip().lower().replace("_", "-")
    if n in ("euc-kr", "euckr", "ks-c-5601-1987", "ksc5601", "x-windows-949", "ms949"):
        return "cp949"
    if n in ("utf8",):
        return "utf-8"
    return n


def _try_decode(raw: bytes, encoding: str) -> str | None:
    """주어진 인코딩으로 디코딩해 보고, 대체문자 비율이 5% 를 넘으면 None."""
    try:
        text = raw.decode(encoding, errors="replace")
    except LookupError:
        return None
    if not text:
        return text
    ratio = text.count("�") / len(text)
    if ratio > REPLACEMENT_RATIO_MAX:
        return None
    return text


def decode_response(resp: requests.Response) -> str:
    """응답 바이트를 문자열로 디코딩한다.

    우선순위: 본문 앞 2KB 의 ``<meta charset>`` / ``<meta http-equiv=content-type>``
    → ``resp.encoding`` → utf-8 → cp949(euc-kr). 각 후보로 디코딩했을 때
    ``\\ufffd`` 비율이 5% 를 넘으면 다음 후보로 넘어간다. 모두 실패하면
    utf-8 + errors="replace" 결과를 돌려준다. 최종 인코딩은 로그로 남긴다.

    Args:
        resp: requests 응답 객체.

    Returns:
        디코딩된 HTML 문자열.
    """
    raw = resp.content or b""
    candidates: list[str] = []
    m = _META_CHARSET_RE.search(raw[:2048])
    if m:
        candidates.append(_normalize_encoding(m.group(1).decode("ascii", "ignore")))
    if resp.encoding:
        candidates.append(_normalize_encoding(resp.encoding))
    candidates.extend(["utf-8", "cp949"])

    seen: set[str] = set()
    for enc in candidates:
        if enc in seen:
            continue
        seen.add(enc)
        text = _try_decode(raw, enc)
        if text is not None:
            logger.info("decode_response: encoding=%s url=%s", enc, getattr(resp, "url", ""))
            return text
    logger.warning("decode_response: 모든 인코딩 후보 실패, utf-8(replace) 사용 url=%s", getattr(resp, "url", ""))
    return raw.decode("utf-8", errors="replace")


def _check_blocked(url: str, text: str) -> None:
    """차단/오류 페이지면 :class:`DartBlockedError` 를 던진다."""
    if len(text) < MIN_BODY_LEN or any(p in text for p in BLOCKED_PHRASES):
        raise DartBlockedError(url, text[:200])


def _throttle() -> None:
    """요청 사이 최소 간격(0.3초)을 보장한다."""
    global _last_request_at
    wait = MIN_INTERVAL_SEC - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def fetch_html(session: requests.Session, url: str) -> str:
    """URL 의 HTML 을 가져온다. 타임아웃 15초, 실패 시 0.5s → 1s → 2s 간격으로 3회 재시도.

    200 이어도 본문이 500자 미만이거나 차단 문구가 있으면 :class:`DartBlockedError`
    (재시도 대상). 요청 사이 최소 0.3초를 띄운다.

    Args:
        session: :func:`build_session` 으로 만든 세션.
        url: 요청 URL.

    Returns:
        디코딩된 HTML 문자열.

    Raises:
        DartBlockedError: 재시도 후에도 차단/오류 페이지인 경우.
        requests.RequestException: 재시도 후에도 네트워크/HTTP 오류인 경우.
    """
    last_exc: Exception | None = None
    for attempt in range(len(RETRY_DELAYS_SEC) + 1):
        if attempt > 0:
            delay = RETRY_DELAYS_SEC[attempt - 1]
            logger.warning("fetch_html 재시도 %d/%d (%.1fs 후) url=%s: %s", attempt, len(RETRY_DELAYS_SEC), delay, url, last_exc)
            time.sleep(delay)
        _throttle()
        try:
            resp = session.get(url, timeout=TIMEOUT_SEC)
            resp.raise_for_status()
            text = decode_response(resp)
            _check_blocked(url, text)
            return text
        except (requests.RequestException, DartBlockedError) as exc:
            last_exc = exc
    assert last_exc is not None
    raise last_exc


def fetch_main(session: requests.Session, rcp_no: str) -> str:
    """``main.do?rcpNo=...`` 페이지 HTML 을 가져온다.

    Args:
        session: 세션.
        rcp_no: 접수번호 14자리.

    Returns:
        main.do HTML (문서 트리 스크립트 포함).
    """
    return fetch_html(session, f"{MAIN_URL}?rcpNo={rcp_no}")
