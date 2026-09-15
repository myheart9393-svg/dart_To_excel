"""공용 pytest fixture. 기본 실행은 네트워크 없이 tests/fixtures/*.html 만 사용한다.

실제 네트워크 테스트는 ``@pytest.mark.network`` 로 표시하며 ``--run-network`` 옵션을 줄 때만 실행된다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--run-network", action="store_true", default=False, help="network 마커 테스트 실행")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "network: 실제 DART 네트워크 호출 (기본 실행에서 제외)")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-network"):
        return
    skip = pytest.mark.skip(reason="--run-network 옵션이 없어 건너뜀")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def fixtures_dir() -> Path:
    """tests/fixtures 디렉터리 경로."""
    return FIXTURES_DIR


@pytest.fixture
def load_fixture(fixtures_dir: Path):
    """fixture HTML 파일을 문자열로 읽는 헬퍼를 돌려준다. 파일이 없으면 skip."""

    def _load(name: str) -> str:
        path = fixtures_dir / name
        if not path.exists():
            pytest.skip(f"fixture 없음: {path}")
        return path.read_text(encoding="utf-8")

    return _load
