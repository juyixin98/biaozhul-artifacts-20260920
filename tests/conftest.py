"""测试夹具：真实 PostgreSQL 独立库 + 每用例重建表 + 临时快照/证据目录。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

TEST_DB_NAME = "recon_acceptance_test"
DEFAULT_MAINT_URL = "postgresql+psycopg2://inbox:inbox@localhost:55432/postgres"
MAINT_URL = os.environ.get("TEST_DATABASE_URL", DEFAULT_MAINT_URL)
TARGET_URL = MAINT_URL.rsplit("/", 1)[0] + "/" + TEST_DB_NAME

# 在导入 app 之前指向测试库
os.environ["DATABASE_URL"] = TARGET_URL
os.environ.setdefault("ADMIN_TOKEN", "")


def _provision_database() -> None:
    admin_engine = create_engine(MAINT_URL, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {TEST_DB_NAME}"))
    admin_engine.dispose()


_provision_database()

from app import db as db_mod  # noqa: E402
from app.demo_crypto import DemoFeeder  # noqa: E402


@pytest.fixture()
def dirs(tmp_path: Path):
    snap = tmp_path / "snapshots"
    evi = tmp_path / "evidence"
    snap.mkdir()
    evi.mkdir()
    os.environ["SNAPSHOT_DIR"] = str(snap)
    os.environ["EVIDENCE_DIR"] = str(evi)
    yield {"snapshots": snap, "evidence": evi}
    os.environ.pop("SNAPSHOT_DIR", None)
    os.environ.pop("EVIDENCE_DIR", None)


@pytest.fixture()
def db(dirs):
    db_mod.init_engine(TARGET_URL)
    db_mod.drop_all()
    db_mod.create_all()
    session = db_mod.db_session()
    yield session
    session.rollback()
    session.close()


@pytest.fixture()
def client(dirs):
    db_mod.init_engine(TARGET_URL)
    db_mod.drop_all()
    db_mod.create_all()
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def feeders():
    return {"chainA": DemoFeeder("feeder-A"), "chainB": DemoFeeder("feeder-B")}
