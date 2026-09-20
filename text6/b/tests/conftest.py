"""Pytest fixtures.

Each test gets an isolated PostgreSQL schema (``test_<uuid>``) with a fresh
Alembic migration, so tests never see each other's data and can run in
parallel. Configure the server with TEST_DATABASE_URL (default points at the
docker-compose database on localhost:5432).
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

# Point settings at the test database before app modules import the engine.
TEST_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg2://consent:consent@localhost:5432/consentvault",
)
os.environ["DATABASE_URL"] = TEST_URL

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from app.database import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ApiKey, Organization, PolicyVersion, Role  # noqa: E402
from app.security import hash_key  # noqa: E402

ADMIN_KEY = "test-admin-key"
AUDITOR_KEY = "test-auditor-key"
ORG_B_ADMIN_KEY = "test-admin-key-b"


@pytest.fixture
def db():
    """Schema-isolated, migrated database + org and API keys."""
    admin_engine = create_engine(TEST_URL, future=True)
    schema = f"test_{uuid.uuid4().hex[:16]}"
    with admin_engine.connect() as conn:
        conn.execute(text("COMMIT"))
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn.commit()

    # Engine scoped to the test schema through the libpq connect-time
    # ``options`` parameter. Unlike a SET on the connect event it survives
    # pool checkouts (SQLAlchemy issues RESET/ROLLBACK on return), and unlike
    # URL encoding it works uniformly with psycopg2.
    engine = create_engine(
        TEST_URL,
        future=True,
        connect_args={"options": f'-c search_path="{schema}"'},
    )

    cfg = Config()
    cfg.set_main_option(
        "script_location", os.path.join(os.path.dirname(__file__), "..", "alembic")
    )
    # Run migrations over a connection from the schema-scoped engine.
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")

    session = Session(bind=engine, future=True)
    org_a = Organization(name="Org A")
    org_b = Organization(name="Org B")
    session.add_all([org_a, org_b])
    session.flush()
    session.add_all(
        [
            ApiKey(organization_id=org_a.id, key_hash=hash_key(ADMIN_KEY),
                   label="admin-a", role=Role.admin),
            ApiKey(organization_id=org_a.id, key_hash=hash_key(AUDITOR_KEY),
                   label="auditor-a", role=Role.auditor),
            ApiKey(organization_id=org_b.id, key_hash=hash_key(ORG_B_ADMIN_KEY),
                   label="admin-b", role=Role.admin),
        ]
    )
    session.add_all(
        [
            PolicyVersion(organization_id=org_a.id, version="v1", body="first policy"),
            PolicyVersion(organization_id=org_a.id, version="v2", body="second policy"),
            PolicyVersion(organization_id=org_b.id, version="v1", body="org b policy"),
        ]
    )
    session.commit()

    yield session

    session.close()
    engine.dispose()
    with admin_engine.connect() as conn:
        conn.execute(text("COMMIT"))
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        conn.commit()
    admin_engine.dispose()


@pytest.fixture
def client(db):
    """TestClient whose request sessions use the isolated test session."""
    def _override_get_db():
        # A fresh Session on the same engine/connection sees the same schema;
        # each request gets its own unit of work.
        request_session = Session(bind=db.bind, future=True)
        try:
            yield request_session
        finally:
            request_session.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def admin_headers():
    return {"X-API-Key": ADMIN_KEY}


@pytest.fixture
def auditor_headers():
    return {"X-API-Key": AUDITOR_KEY}


@pytest.fixture
def org_b_headers():
    return {"X-API-Key": ORG_B_ADMIN_KEY}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def create_subject(client, headers, **over):
    payload = {"external_ref": f"ref-{uuid.uuid4().hex[:10]}", "email": "a@b.co"}
    payload.update(over)
    r = client.post("/v1/subjects", headers=headers, json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def grant(
    client,
    headers,
    subject_id,
    purpose,
    *,
    expected_version=0,
    policy_version="v1",
    expires_at=None,
    event_id=None,
):
    event_id = event_id or f"evt-{uuid.uuid4().hex[:12]}"
    body = {
        "event_id": event_id,
        "subject_id": subject_id,
        "purpose": purpose,
        "expected_version": expected_version,
        "policy_version": policy_version,
    }
    if expires_at is not None:
        body["expires_at"] = expires_at.isoformat() if isinstance(expires_at, datetime) else expires_at
    r = client.post("/v1/consent/grant", headers=headers, json=body)
    return r, event_id


def utc(dt):
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
