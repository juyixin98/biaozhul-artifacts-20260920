import os

import pytest

os.environ.setdefault(
    "SENTINEL_DATABASE_URL",
    "postgresql+psycopg://sentinel:sentinel@localhost:5432/sentinel_b_test",
)
os.environ.setdefault("SENTINEL_JWT_SECRET", "test-secret")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.detection.engine import ingest_batch  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    DepartmentMembership,
    Device,
    Organization,
    User,
    UserRole,
)
from app.security import hash_password  # noqa: E402

settings = get_settings()
engine = create_engine(settings.database_url, isolation_level="READ COMMITTED", future=True)
TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def _clean_tables():
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE alert_investigations, alerts, baselines, events, "
                          "devices, department_memberships, users, organizations RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def db():
    s = TestSession()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def client(db):
    from app.database import get_db

    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# data builders
# ---------------------------------------------------------------------------

def make_org(db, oid, name="Org", tz="UTC", parent_id=None, path=None):
    if path is None:
        parent_org = db.get(Organization, parent_id) if parent_id else None
        prefix = parent_org.path if parent_org else "/"
        path = f"{prefix}{oid}/"
    org = Organization(id=oid, name=name, timezone=tz, parent_id=parent_id, path=path)
    db.add(org)
    db.flush()
    return org


def make_user(db, uid, username, role, org_id=None, manager_id=None, password="Passw0rd!"):
    u = User(
        id=uid, username=username, password_hash=hash_password(password),
        full_name=username.title(), role=role, org_id=org_id, manager_id=manager_id,
    )
    db.add(u)
    db.flush()
    return u


def make_device(db, key, user_id, name="dev"):
    d = Device(device_key=key, user_id=user_id, name=name)
    db.add(d)
    db.flush()
    return d


def auth_headers(client, username, password="Passw0rd!"):
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def event(event_id, device_key, ev_type, occurred_at, payload=None):
    return {
        "event_id": event_id,
        "device_key": device_key,
        "type": ev_type,
        "occurred_at": occurred_at.isoformat(),
        "payload": payload or {},
    }
