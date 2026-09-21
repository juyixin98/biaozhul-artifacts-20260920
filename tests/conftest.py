import os

os.environ["DATABASE_URL"] = (
    "postgresql+psycopg2://sentinel:sentinel@127.0.0.1:5432/sentinel_test_run"
)
os.environ["JWT_SECRET"] = "test-secret"

from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    AnalystDepartment,
    Department,
    Device,
    Employee,
    Organization,
    User,
)
from app.security import hash_password  # noqa: E402

TEST_URL = os.environ["DATABASE_URL"]
engine = create_engine(TEST_URL)
TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

SH = ZoneInfo("Asia/Shanghai")


def sh(y, m, d, h, mi=0, s=0) -> datetime:
    """Shanghai-local aware datetime."""
    return datetime(y, m, d, h, mi, s, tzinfo=SH)


@pytest.fixture(scope="session", autouse=True)
def create_schema():
    with engine.connect() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.commit()
    Base.metadata.create_all(engine)
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_tables():
    yield
    with engine.connect() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
        conn.commit()


@pytest.fixture
def db():
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    def override_get_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


class Seed:
    pass


@pytest.fixture
def seed(db):
    """Org (Asia/Shanghai), Engineering/Finance departments, employees and users.

    mandy manages alice, bob (Engineering) and carol (Finance). eve (Finance)
    has no manager. analyst is authorized for Engineering only.
    """
    s = Seed()
    org = Organization(name="Test Org", timezone="Asia/Shanghai")
    db.add(org)
    db.flush()
    eng = Department(org_id=org.id, name="Engineering")
    fin = Department(org_id=org.id, name="Finance")
    db.add_all([eng, fin])
    db.flush()

    mandy = Employee(org_id=org.id, department_id=eng.id, name="Mandy")
    db.add(mandy)
    db.flush()
    alice = Employee(org_id=org.id, department_id=eng.id, name="Alice", manager_id=mandy.id)
    bob = Employee(org_id=org.id, department_id=eng.id, name="Bob", manager_id=mandy.id)
    carol = Employee(org_id=org.id, department_id=fin.id, name="Carol", manager_id=mandy.id)
    eve = Employee(org_id=org.id, department_id=fin.id, name="Eve")
    db.add_all([alice, bob, carol, eve])
    db.flush()

    admin = User(username="admin", password_hash=hash_password("pw"), role="admin")
    manager = User(username="manager", password_hash=hash_password("pw"),
                   role="manager", employee_id=mandy.id)
    analyst = User(username="analyst", password_hash=hash_password("pw"), role="analyst")
    db.add_all([admin, manager, analyst])
    db.flush()
    db.add(AnalystDepartment(user_id=analyst.id, department_id=eng.id))

    s.dev_alice = Device(employee_id=alice.id, hostname="alice-1")
    s.dev_bob = Device(employee_id=bob.id, hostname="bob-1")
    s.dev_carol = Device(employee_id=carol.id, hostname="carol-1")
    s.dev_eve = Device(employee_id=eve.id, hostname="eve-1")
    db.add_all([s.dev_alice, s.dev_bob, s.dev_carol, s.dev_eve])
    db.flush()

    s.org, s.eng, s.fin = org, eng, fin
    s.mandy, s.alice, s.bob, s.carol, s.eve = mandy, alice, bob, carol, eve
    s.admin, s.manager, s.analyst = admin, manager, analyst
    db.commit()
    return s


def login(client, username, password="pw") -> dict:
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def ev(device_id, event_id, event_type, occurred_at, payload=None):
    return {
        "device_id": device_id,
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": occurred_at.isoformat(),
        "payload": payload or {},
    }


def post_batch(client, headers, events):
    return client.post("/api/events/batch", json={"events": events}, headers=headers)
