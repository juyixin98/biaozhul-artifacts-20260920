"""Pytest fixtures: isolated temp DB + TestClient for every test."""

from __future__ import annotations

import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEMO_DBC = (ROOT / "examples" / "demo.dbc").read_text(encoding="utf-8")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CANDECODE_DB", str(tmp_path / "test_candecode.db"))
    from app import main

    main._db_cache.clear()
    main._store = None  # force get_store() to open the temp DB
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def uploaded_client(client):
    response = client.post("/dbc", json={"dbc": DEMO_DBC})
    assert response.status_code == 200, response.text
    return client


@pytest.fixture
def version_id(uploaded_client):
    return uploaded_client.post("/dbc", json={"dbc": DEMO_DBC}).json()[
        "version_id"
    ]
