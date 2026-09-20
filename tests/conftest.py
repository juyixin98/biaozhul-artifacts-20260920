import os
import uuid

import psycopg2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.db import get_db
from app.main import app
from app.models import (
    Base,
    Organization,
    PolicyVersion,
    Purpose,
    User,
    utcnow,
)

ADMIN_DSN = "host=localhost port=55435 user=consentvault password=consentvault dbname=consentvault"
TEST_DB_URL = "postgresql+psycopg2://consentvault:consentvault@localhost:55435/consentvault_test"

ADMIN_TOKEN = "test-admin-token"
AUDITOR_TOKEN = "test-auditor-token"
ORG2_ADMIN_TOKEN = "test-org2-admin-token"


@pytest.fixture(scope="session")
def engine():
    conn = psycopg2.connect(ADMIN_DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP DATABASE IF EXISTS consentvault_test WITH (FORCE)")
        cur.execute("CREATE DATABASE consentvault_test")
    conn.close()
    eng = create_engine(TEST_DB_URL)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db_session(engine):
    """每个测试用例前清空所有表。"""
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(text(f'TRUNCATE TABLE "{table.name}" CASCADE'))
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def seeded(db_session):
    org1 = Organization(name=f"org1-{uuid.uuid4().hex[:8]}")
    org2 = Organization(name=f"org2-{uuid.uuid4().hex[:8]}")
    db_session.add_all([org1, org2])
    db_session.flush()
    admin = User(org_id=org1.id, email="a@o1.test", role="admin", token=ADMIN_TOKEN)
    auditor = User(org_id=org1.id, email="r@o1.test", role="auditor", token=AUDITOR_TOKEN)
    admin2 = User(org_id=org2.id, email="a@o2.test", role="admin", token=ORG2_ADMIN_TOKEN)
    db_session.add_all([admin, auditor, admin2])
    purpose = Purpose(org_id=org1.id, code="marketing", name="Marketing")
    db_session.add(purpose)
    db_session.flush()
    pv1 = PolicyVersion(org_id=org1.id, purpose_id=purpose.id, version=1,
                        content="v1", status="published", published_at=utcnow())
    db_session.add(pv1)
    db_session.commit()
    return {"org1": org1, "org2": org2, "purpose": purpose, "pv1": pv1}


@pytest.fixture()
def client(engine):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def admin(client, seeded):
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture()
def auditor(client, seeded):
    return {"Authorization": f"Bearer {AUDITOR_TOKEN}"}


@pytest.fixture()
def org2_admin(client, seeded):
    return {"Authorization": f"Bearer {ORG2_ADMIN_TOKEN}"}


def grant_payload(event_id, subject_ref, pv_id, expected_version, **kw):
    body = {
        "event_id": event_id,
        "event_type": "grant",
        "subject_ref": subject_ref,
        "purpose_code": "marketing",
        "policy_version_id": str(pv_id),
        "expected_version": expected_version,
    }
    body.update(kw)
    return body


def withdraw_payload(event_id, subject_ref, expected_version):
    return {
        "event_id": event_id,
        "event_type": "withdraw",
        "subject_ref": subject_ref,
        "purpose_code": "marketing",
        "expected_version": expected_version,
    }
