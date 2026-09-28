"""Test configuration: marks, paths, sys.path bootstrap."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "fixtures" / "corpora"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "unit: kernel/unit tests")
    config.addinivalue_line("markers", "integration: index/service tests")
    config.addinivalue_line("markers", "e2e: end-to-end HTTP tests")
    config.addinivalue_line("markers", "oracle: cross-checks vs the independent oracle")


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES
