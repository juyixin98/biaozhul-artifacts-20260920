"""Pytest configuration: per-test app backed by a temp SQLite database."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    return Settings(
        db_path=str(tmp_path / "test_index.db"),
        chunk_size=16,
        log_level="WARNING",
        redact_snippets=True,
    )


@pytest.fixture
def app(settings):
    application = create_app(settings)
    yield application
    application.state.app_state.service._db.close()


@pytest.fixture
def service(app):
    return app.state.app_state.service


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c
