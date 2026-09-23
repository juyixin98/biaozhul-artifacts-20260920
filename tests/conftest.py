"""Shared pytest configuration: isolate the SQLite database per test session."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="clm-test-")
os.environ["CLM_DB_PATH"] = str(Path(_TMP) / "test.db")


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app import db
    from app.main import app

    db.init_db()
    # Isolate tests: pools/quotes are write-once, so start each from a clean slate.
    with db.get_conn() as conn:
        conn.execute("DELETE FROM quotes")
        conn.execute("DELETE FROM pools")
    with TestClient(app) as c:
        yield c
