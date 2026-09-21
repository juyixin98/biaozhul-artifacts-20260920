import os

# Must be set before any app module reads settings / builds the engine.
os.environ.setdefault(
    "CAREFORCE_DATABASE_URL",
    "postgresql+psycopg2:///careforce_test?host=/var/run/postgresql",
)

from datetime import datetime, time, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.clock import clock  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402

UTC = timezone.utc
FROZEN = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)  # a Monday


@pytest.fixture(scope="session", autouse=True)
def _migrated():
    # Build the test schema from the real Alembic migration every session,
    # whatever state a previous session left behind.
    cfg = Config()
    cfg.set_main_option("script_location", "alembic")
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    Base.metadata.drop_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def db():
    """Clean database around each test. API endpoints open their own engine
    sessions, so truncation (rather than a wrapped transaction) is used."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
        session.close()


@pytest.fixture(autouse=True)
def frozen_clock():
    """Freeze the business clock at a fixed Monday for every test."""
    clock.set(FROZEN)
    yield clock
    clock.reset()


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def advance(frozen_clock):
    """Advance the frozen clock by a timedelta."""

    def _advance(value: timedelta):
        frozen_clock.set(frozen_clock.now() + value)
        return frozen_clock.now()

    return _advance


# --- object factory ---------------------------------------------------------


from app.models import (  # noqa: E402
    Assignment,
    AssignmentStatus,
    CarePlan,
    CareWorker,
    Coordinator,
    PlanStatus,
    Qualification,
    Recurrence,
    Task,
    TaskStatus,
    Unit,
    coordinator_units,
)


@pytest.fixture
def make_unit(db):
    def _make(name="Test Unit"):
        unit = Unit(name=name)
        db.add(unit)
        db.flush()
        return unit

    return _make


@pytest.fixture
def make_coordinator(db):
    def _make(name="Coord", units=None):
        coordinator = Coordinator(name=name, units=units or [],
                                  created_at=FROZEN)
        db.add(coordinator)
        db.flush()
        return coordinator

    return _make


@pytest.fixture
def make_worker(db):
    def _make(unit, name="Worker", tz="UTC", qualifications=None, active=True):
        worker = CareWorker(
            name=name, unit_id=unit.id, timezone=tz, active=active,
            created_at=FROZEN,
        )
        db.add(worker)
        db.flush()
        for code, valid_from, valid_until in qualifications or []:
            db.add(Qualification(
                worker_id=worker.id, code=code,
                valid_from=valid_from, valid_until=valid_until,
            ))
        db.flush()
        return worker

    return _make


@pytest.fixture
def make_plan(db):
    def _make(unit, *, recipient="r1", tz="UTC", recurrence=Recurrence.daily,
              day_of_week=None, window_start=time(8, 0), window_end=time(10, 0),
              duration=60, qualifications=None, status=PlanStatus.active,
              version=1):
        plan = CarePlan(
            status=status,
            care_recipient_id=recipient,
            unit_id=unit.id,
            service_timezone=tz,
            recurrence=recurrence,
            day_of_week=day_of_week,
            window_start=window_start,
            window_end=window_end,
            duration_minutes=duration,
            required_qualifications=qualifications or [],
            version=version,
            created_at=FROZEN,
            updated_at=FROZEN,
        )
        db.add(plan)
        db.flush()
        return plan

    return _make


@pytest.fixture
def make_task(db):
    def _make(plan, *, start, end, status=TaskStatus.pending, occurrence=None,
              prerequisite_task_ids=None, version=None):
        task = Task(
            plan_id=plan.id,
            plan_version=version if version is not None else plan.version,
            care_recipient_id=plan.care_recipient_id,
            unit_id=plan.unit_id,
            occurrence_key=occurrence or start.date().isoformat(),
            starts_at=start,
            ends_at=end,
            status=status,
            prerequisite_task_ids=prerequisite_task_ids or [],
            created_at=FROZEN,
            updated_at=FROZEN,
        )
        db.add(task)
        db.flush()
        return task

    return _make


@pytest.fixture
def make_assignment(db):
    def _make(task, worker, *, status=AssignmentStatus.assigned,
              invited_at=None, expires_at=None, created_at=None):
        now = clock.now()
        assignment = Assignment(
            task_id=task.id,
            worker_id=worker.id,
            status=status,
            invited_at=invited_at or now,
            expires_at=expires_at or (now + timedelta(minutes=8)),
            created_via="auto",
            created_at=created_at or now,
        )
        db.add(assignment)
        db.flush()
        return assignment

    return _make
