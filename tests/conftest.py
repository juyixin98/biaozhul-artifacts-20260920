"""Shared test fixtures: isolated SQLite databases per test."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bracket_index.api.app import create_app
from bracket_index.config import Settings
from bracket_index.index.engine import IndexEngine
from bracket_index.index.store import Store

SMALL_CHUNK = 8  # tiny chunks so tests exercise cross-chunk composition


@pytest.fixture()
def engine(tmp_path) -> IndexEngine:
    store = Store(str(tmp_path / "index.db"))
    eng = IndexEngine(store, chunk_size=SMALL_CHUNK)
    yield eng
    store.close()


@pytest.fixture()
def client(tmp_path) -> TestClient:
    settings = Settings(
        db_path=str(tmp_path / "api.db"), chunk_size=SMALL_CHUNK, log_level="WARNING"
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client
