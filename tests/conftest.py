"""Shared pytest fixtures: isolated SQLite DB + authenticated API client."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Point at a throwaway DB before application import.
_tmp = tempfile.mkdtemp(prefix="p070-tests-")
os.environ.setdefault("DISPATCH_DB", str(Path(_tmp) / "init.db"))
os.environ.setdefault("DISPATCH_SECRET", "test-secret-key-for-unit-tests")

from app import config  # noqa: E402
from app.main import app  # noqa: E402
from app.database import init_db  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "dispatch.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    init_db()

    with TestClient(app) as c:
        # each test gets its own operator
        c.post("/auth/register",
               json={"username": "tester", "password": "secret-password"})
        resp = c.post("/auth/login",
                      json={"username": "tester",
                            "password": "secret-password"})
        token = resp.json()["access_token"]
        c.headers.update({"X-Auth-Token": token})
        yield c
