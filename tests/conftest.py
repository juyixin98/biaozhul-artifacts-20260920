"""Pytest fixtures.

A dedicated PostgreSQL database (``CV_TEST_DATABASE_URL``) is migrated once
per session; each test runs against truncated tables for isolation.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

# Must be set before application modules import settings.
os.environ.setdefault(
    "CV_DATABASE_URL",
    "postgresql+psycopg2://consentvault:consentvault@localhost:5432/consentvault_test",
)

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from app.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ApiKey, Organization  # noqa: E402
from app.security import generate_key, hash_key  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _migrated() -> Iterator[None]:
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def _clean_tables() -> Iterator[None]:
    # TRUNCATE is allowed even on immutable tables (the triggers block
    # UPDATE/DELETE row operations, not TRUNCATE), and CASCADE resets FKs.
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE TABLE organizations RESTART IDENTITY CASCADE"
            )
        )
    # The lru_cached settings object is fine across tests.
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _make_org_with_keys(name: str) -> dict[str, str]:
    db = SessionLocal()
    try:
        org = Organization(name=name)
        db.add(org)
        db.flush()
        admin_raw = generate_key()
        auditor_raw = generate_key()
        db.add_all(
            [
                ApiKey(
                    organization_id=org.id,
                    key_hash=hash_key(admin_raw),
                    label="admin",
                    role="admin",
                ),
                ApiKey(
                    organization_id=org.id,
                    key_hash=hash_key(auditor_raw),
                    label="auditor",
                    role="auditor",
                ),
            ]
        )
        db.commit()
        return {"admin": admin_raw, "auditor": auditor_raw, "org": name, "org_id": org.id}
    finally:
        db.close()


@pytest.fixture
def org() -> dict[str, str]:
    return _make_org_with_keys(f"org-{uuid.uuid4().hex[:8]}")


@pytest.fixture
def org2() -> dict[str, str]:
    return _make_org_with_keys(f"org-{uuid.uuid4().hex[:8]}")


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}
