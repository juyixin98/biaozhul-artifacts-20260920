"""Shared test fixtures."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("CAN_DECODER_DB", str(ROOT / "data" / "test.db"))

from app.main import app  # noqa: E402
from app.storage import Store  # noqa: E402

DBC_TEXT = (ROOT / "examples" / "powertrain.dbc").read_text()


@pytest.fixture()
def tmp_db(tmp_path):
    path = tmp_path / "test.db"
    store = Store(str(path))
    store.add_dbc("powertrain", DBC_TEXT, "sha")
    yield store
    store.close()


@pytest.fixture()
def client(tmp_db):
    from fastapi.testclient import TestClient

    app.state.store = tmp_db
    with TestClient(app) as c:
        yield c
