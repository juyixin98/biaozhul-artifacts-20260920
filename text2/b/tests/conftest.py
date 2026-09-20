"""Pytest configuration.

Tests run against a real PostgreSQL database (the row locks and partial
unique indexes that make concurrent accepts safe do not exist on SQLite). The
URL is taken from ``TEST_DATABASE_URL`` and defaults to the local dev
instance; schema is created with ``Base.metadata.create_all`` (identical DDL
to the Alembic initial migration — both are exercised in the test suite).

A :class:`FakeClock` is installed on the app so invitation expiry and
generation boundaries are fully deterministic.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text

os.environ.setdefault(
    "TEST_DATABASE_URL",
    "postgresql+psycopg2://postgres:postgres@localhost:15432/careforce_test",
)

from app.clock import FakeClock  # noqa: E402
from app.db import Base, configure_engine, get_engine  # noqa: E402
from app.main import create_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# Import models so metadata is populated.
import app.models  # noqa: E402,F401


FIXED_NOW = datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)  # Monday 00:00 UTC


def _create_database_if_missing() -> None:
    url = os.environ["TEST_DATABASE_URL"]
    # Connect to the server default DB to CREATE/DROP the test database.
    head, db_name = url.rsplit("/", 1)
    admin_url = head + "/postgres"
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        exists = conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": db_name},
        )
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
        else:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": db_name},
            )
    engine.dispose()


@pytest.fixture(scope="session")
def engine():
    _create_database_if_missing()
    eng = configure_engine(os.environ["TEST_DATABASE_URL"])
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine):
    """Clean tables before each test and yield a session."""
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    from sqlalchemy.orm import Session

    session = Session(engine)
    # Deterministic clock for service-layer tests.
    session.info["now"] = FIXED_NOW
    yield session
    session.close()


@pytest.fixture()
def clock():
    return FakeClock(start=FIXED_NOW)


@pytest.fixture()
def app(engine, clock):
    application = create_app()
    application.state.clock = clock
    yield application


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Domain-object factory
# ---------------------------------------------------------------------------

@pytest.fixture()
def factory(db, clock):
    """Small builder used by every test to set up the world."""

    class Factory:
        def __init__(self):
            from app.models import (
                Coordinator,
                Qualification,
                Unit,
                Worker,
            )

            self.Unit = Unit
            self.Worker = Worker
            self.Coordinator = Coordinator
            self.Qualification = Qualification
            self.now = clock.now()

        def unit(self, name="unit-1", tz="UTC"):
            from app.models import Unit

            u = Unit(name=name, timezone=tz)
            db.add(u)
            db.flush()
            return u

        def coordinator(self, external_id="coord-1", units=None, name="协调员"):
            from app.models import Coordinator

            c = Coordinator(name=name, external_id=external_id)
            if units is not None:
                c.units = list(units)
            db.add(c)
            db.flush()
            return c

        def worker(self, name=None, external_id=None, tz="UTC", units=None,
                   active=True):
            from app.models import Worker

            wid = getattr(self, "_worker_seq", 1)
            self._worker_seq = wid + 1
            w = Worker(
                name=name or f"worker-{wid}",
                external_id=external_id or f"worker-{wid}",
                timezone=tz,
                active=active,
            )
            if units is not None:
                w.units = list(units)
            db.add(w)
            db.flush()
            return w

        def qualification(self, code=None, name="qual"):
            from app.models import Qualification

            qid = getattr(self, "_qual_seq", 1)
            self._qual_seq = qid + 1
            q = Qualification(code=code or f"Q{qid}", name=name)
            db.add(q)
            db.flush()
            return q

        def grant(self, worker, qualification, *, valid_from=None,
                  valid_until=None, revoked=False):
            from app.models import WorkerQualification

            cred = WorkerQualification(
                worker_id=worker.id,
                qualification_id=qualification.id,
                valid_from=valid_from or (self.now - __import__("datetime").timedelta(days=30)),
                valid_until=valid_until,
                revoked=revoked,
            )
            db.add(cred)
            db.flush()
            return cred

        def plan(
            self,
            unit,
            *,
            title="plan",
            timezone="UTC",
            period_days=7,
            anchor_date=None,
            slots=None,
            qualification_ids=None,
            prerequisite_plan_ids=None,
            coordinator=None,
        ):
            from app.services.plans import create_plan

            if slots is None:
                # Monday 09:00, one hour, window 09:00–10:00.
                slots = [
                    {
                        "weekday": 0,
                        "start_at": "09:00",
                        "latest_start_at": "10:00",
                        "duration_minutes": 60,
                    }
                ]
            if anchor_date is None:
                anchor_date = self.now.astimezone(
                    __import__("zoneinfo").ZoneInfo(timezone)
                ).date()
            if coordinator is None:
                coordinator = self.coordinator(units=[unit])
            return create_plan(
                db,
                clock,
                unit_id=unit.id,
                title=title,
                timezone=timezone,
                period_days=period_days,
                anchor_date=anchor_date,
                slots=slots,
                qualification_ids=qualification_ids or [],
                prerequisite_plan_ids=prerequisite_plan_ids or [],
                coordinator_id=coordinator.id,
            )

    return Factory()
