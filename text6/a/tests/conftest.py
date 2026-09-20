"""Test fixtures: scratch PostgreSQL database + FastAPI test client.

A fresh database ``consentvault_test`` is created (and recreated) per session,
migrations are applied through Alembic, and the app's engine is rebound to it.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

# Must be set before importing app modules.
ADMIN_URL = os.environ.get(
    "TEST_POSTGRES_ADMIN_URL",
    "postgresql+psycopg2://consent:consent@localhost:55435/postgres",
)
TEST_DB = os.environ.get("TEST_POSTGRES_DB", "consentvault_test")
_SERVER, _DBPART = ADMIN_URL.rsplit("/", 1)
TEST_URL = f"{_SERVER}/{TEST_DB}"

os.environ.setdefault("CONSENTVAULT_DATABASE_URL", TEST_URL)
os.environ.setdefault("CONSENTVAULT_MANAGEMENT_API_KEY", "test-mgmt-key")
os.environ.setdefault("CONSENTVAULT_AUDITOR_API_KEY", "test-auditor-key")
os.environ.setdefault("CONSENTVAULT_ENVIRONMENT", "test")


def _create_scratch_database() -> None:
    engine = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", future=True)
    with engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {TEST_DB}"))
    engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _database() -> Iterator[None]:
    _create_scratch_database()
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_URL)
    command.upgrade(cfg, "head")
    yield
    engine = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", future=True)
    with engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
    engine.dispose()


@pytest.fixture()
def client() -> Iterator[TestClient]:
    # The app engine was constructed against CONSENTVAULT_DATABASE_URL, which
    # conftest pins to the scratch database before app import.
    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_tables(_database):
    """Truncate mutable application data between tests (migration objects stay)."""
    from app.db import engine
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("SET session_replication_role = replica"))
        for tbl in (
            "audit_logs", "event_idempotency", "consent_events",
            "consent_states", "policy_versions", "consent_policies",
            "subject_export_copies", "subjects", "purposes",
            "api_keys", "organizations",
        ):
            conn.execute(text(f"TRUNCATE TABLE {tbl} RESTART IDENTITY CASCADE"))
        conn.execute(text("SET session_replication_role = default"))
    yield


@pytest.fixture()
def make_org(client: TestClient):
    """Factory creating a fresh organization; returns dict with keys/headers."""
    def _make(name: str | None = None) -> dict:
        name = name or f"org-{uuid.uuid4().hex[:10]}"
        r = client.post(
            "/management/organizations",
            json={"name": name},
            headers={"X-Management-Key": "test-mgmt-key"},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        return {
            "id": body["organization_id"],
            "name": name,
            "admin_key": body["admin_api_key"],
            "auditor_key": body["auditor_api_key"],
            "admin": {"X-API-Key": body["admin_api_key"]},
            "auditor": {"X-API-Key": body["auditor_api_key"]},
        }

    return _make


@pytest.fixture()
def org(make_org) -> dict:
    return make_org()


@pytest.fixture()
def purpose(client, org):
    r = client.post(
        "/api/v1/purposes",
        json={"key": "marketing", "description": "d"},
        headers=org["admin"],
    )
    assert r.status_code == 201, r.text
    return "marketing"


@pytest.fixture()
def policy_v1(client, org, purpose) -> int:
    r = client.post(
        f"/api/v1/purposes/{purpose}/policy-versions",
        json={"body": "v1 body"},
        headers=org["admin"],
    )
    assert r.status_code == 201, r.text
    return r.json()["version"]
