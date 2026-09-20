from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.clock import Clock
from app.database import Base, get_db
from app.main import app


class FakeClock(Clock):
    """可控时钟：测试用 advance() 推进时间。"""

    def __init__(self, now: datetime):
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        self._now += timedelta(**kwargs)


# 2026-09-14 是周一 (ISO W38)
BASE_NOW = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False,
                                  expire_on_commit=False)
    Base.metadata.create_all(engine)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def env(db_session):
    clock = FakeClock(BASE_NOW)
    app.state.clock = clock

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client, clock, db_session
    app.dependency_overrides.clear()


# ------------------------------------------------------------ 测试辅助

def make_caregiver(client, name="CG", unit="unit-a"):
    r = client.post("/caregivers", json={"name": name, "unit_id": unit})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def make_qualification(client, caregiver_id, code="RN",
                       valid_from="2026-01-01T00:00:00Z",
                       valid_until="2027-01-01T00:00:00Z"):
    r = client.post(f"/caregivers/{caregiver_id}/qualifications",
                    json={"code": code, "valid_from": valid_from,
                          "valid_until": valid_until})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def make_plan(client, unit="unit-a", qualification="RN", **overrides):
    body = {
        "name": "plan", "unit_id": unit, "patient_name": "P",
        "timezone": "UTC", "frequency": "daily", "interval": 1,
        "window_start": "09:00:00", "window_end": "12:00:00",
        "duration_minutes": 60, "required_qualification": qualification,
        "start_date": "2026-09-14",
    }
    body.update(overrides)
    r = client.post("/plans", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def generate(client, plan_id):
    r = client.post(f"/plans/{plan_id}/generate")
    assert r.status_code == 200, r.text
    return r.json()
