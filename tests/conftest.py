"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from atms_backend.core.budgets import Budgets  # noqa: E402
from atms_backend.services.engine_service import ATMService  # noqa: E402
from atms_backend.storage.repository import Repository  # noqa: E402
from atms_backend.storage.schema import connect, initialize  # noqa: E402

FIXTURES = ROOT / "fixtures"


@pytest.fixture
def service(tmp_path):
    conn = connect(tmp_path / "test.db")
    initialize(conn)
    yield ATMService(Repository(conn), Budgets())
    conn.close()


@pytest.fixture
def fixture_source():
    def _load(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return _load
