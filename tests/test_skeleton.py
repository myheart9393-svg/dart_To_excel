"""모든 모듈이 import 되는지 검증한다 (Step 0 뼈대 테스트의 잔여분)."""

from __future__ import annotations

import importlib

import pytest

MODULES = [
    "dart.models",
    "dart.fetcher",
    "dart.tree",
    "dart.tables",
    "dart.statements",
    "dart.notes",
    "dart.excel",
    "dart.pipeline",
]


@pytest.mark.parametrize("module_name", MODULES)
def test_module_imports(module_name: str) -> None:
    assert importlib.import_module(module_name) is not None
