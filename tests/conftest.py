"""pytest 共享夹具：每个测试使用临时 SQLite 与真实夹具文件。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.engine import load_fixture
from app.main import app, FIXTURE_PATH

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def vulns_and_meta():
    return load_fixture(FIXTURE_PATH)


@pytest.fixture()
def vulns(vulns_and_meta):
    return vulns_and_meta[0]


@pytest.fixture()
def fixture_meta(vulns_and_meta):
    return vulns_and_meta[1]


@pytest.fixture()
def conn(tmp_path):
    c = dbmod.connect(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 让 API 使用临时数据库，避免污染 data/sbom.db
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "DB_PATH", tmp_path / "api.db")
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def full_sbom():
    return json.loads((ROOT / "examples" / "sbom-full.json").read_text("utf-8"))


@pytest.fixture()
def minimal_sbom():
    return json.loads((ROOT / "examples" / "sbom-minimal.json").read_text("utf-8"))
