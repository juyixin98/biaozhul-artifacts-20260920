"""Pytest fixtures: isolated DB per test session, clock-controlled app, helpers."""
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

# Must be set BEFORE app.config is imported.
TEST_DB_URL = os.environ.get(
    "CAREFORCE_TEST_DATABASE_URL",
    "postgresql+psycopg2://careforce:careforce@localhost:5432/careforce_test",
)
os.environ["CAREFORCE_DATABASE_URL"] = TEST_DB_URL
os.environ["CAREFORCE_REAPER_INTERVAL_SECONDS"] = "0"  # tests drive expiry explicitly

from app.config import get_settings  # noqa: E402
from app.db import Base  # noqa: E402
import app.models  # noqa: E402,F401
from app.main import create_app  # noqa: E402
from app.models import (  # noqa: E402
    CarePlan,
    Coordinator,
    CoordinatorUnitGrant,
    Qualification,
    Unit,
    Worker,
    WorkerQualification,
)

get_settings.cache_clear()

engine = create_engine(TEST_DB_URL, poolclass=NullPool, future=True)
TestSession = sessionmaker(bind=engine, expire_on_commit=False, future=True)

CNA = "CNA"
LIFT = "LIFT"


@pytest.fixture(scope="session", autouse=True)
def _schema() -> Iterator[None]:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture()
def db() -> Iterator[Session]:
    """Clean tables before each test for full isolation."""
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
        conn.execute(text("SELECT setval(pg_get_serial_sequence('units','id'), 1, false)"))
    session = TestSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db, frozen_now: datetime) -> Iterator[TestClient]:
    from app.deps import get_session

    app = create_app()

    def _override_session() -> Iterator[Session]:
        # Same engine/session the test uses; the app commits at request end.
        yield db

    app.dependency_overrides[get_session] = _override_session
    app.state.clock.freeze(frozen_now)
    with TestClient(app) as c:
        c._clock = app.state.clock
        yield c
    app.dependency_overrides.clear()


# ---------- domain factory helpers ----------

@pytest.fixture()
def frozen_now() -> datetime:
    # Monday 2026-09-21 00:00 UTC = Monday 08:00 Shanghai.
    return datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)


def make_unit(db: Session, name: str = "U1") -> Unit:
    unit = Unit(name=f"{name}-{datetime.now().microsecond}")
    db.add(unit)
    db.flush()
    return unit


def make_coordinator(db: Session, *, admin: bool = False, unit: Unit | None = None) -> Coordinator:
    c = Coordinator(name=f"coord-{datetime.now().microsecond}", is_admin=admin)
    db.add(c)
    db.flush()
    if unit is not None and not admin:
        db.add(CoordinatorUnitGrant(coordinator_id=c.id, unit_id=unit.id))
        db.flush()
    return c


def make_worker(db: Session, unit: Unit, name: str | None = None, active: bool = True) -> Worker:
    w = Worker(name=name or f"w-{datetime.now().microsecond}", unit_id=unit.id, active=active)
    db.add(w)
    db.flush()
    return w


def make_qualification(db: Session, code: str) -> Qualification:
    q = Qualification(code=code, name=code)
    db.add(q)
    db.flush()
    return q


def grant_qualification(
    db: Session,
    worker: Worker,
    qual: Qualification,
    *,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
) -> WorkerQualification:
    wq = WorkerQualification(
        worker_id=worker.id,
        qualification_id=qual.id,
        valid_from=valid_from or datetime(2000, 1, 1, tzinfo=timezone.utc),
        valid_until=valid_until,
    )
    db.add(wq)
    db.flush()
    return wq


def coord_headers(coordinator: Coordinator) -> dict:
    return {"X-Coordinator-Id": str(coordinator.id)}


def worker_headers(worker: Worker) -> dict:
    return {"X-Worker-Id": str(worker.id)}
