"""Test fixtures.

Each test session rebuilds the *isolated* PostgreSQL test database from
the Alembic migration (so the tested schema is exactly the deployed one),
and each test starts from a clean table set.

The background sweeper is disabled (ENABLE_SWEEPER=false): tests drive
expiry deterministically through the controllable-clock + sweep API.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import get_settings
from app.database import SessionLocal, engine

# Ensure the test DB URL is in effect before app modules cache settings.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://skillpulse:skillpulse@localhost:5432/skillpulse_app_test",
)
os.environ.setdefault("ENABLE_SWEEPER", "false")


def _truncate_all() -> None:
    # Reflect actual table names from the DB and reset everything except
    # the singleton app_clock row.
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE TABLE result_corrections, certificates, step_results, "
                "enrollments, courses, step_prerequisites, steps, "
                "program_versions, programs, users RESTART IDENTITY CASCADE"
            )
        )
        conn.execute(text("UPDATE app_clock SET offset_seconds = 0 WHERE id = 1"))
        conn.execute(
            text("INSERT INTO app_clock (id, offset_seconds) VALUES (1, 0) "
                 "ON CONFLICT (id) DO NOTHING")
        )


@pytest.fixture(scope="session", autouse=True)
def _prepare_database():
    # Apply migrations via Alembic subprocess-equivalent API once per session.
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def clean_db():
    _truncate_all()
    yield
    SessionLocal.remove() if hasattr(SessionLocal, "remove") else None


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SUPERVISOR_H = {"X-User-Id": "1"}
LEARNER_HEADERS = [
    {"X-User-Id": str(i)} for i in range(10, 100)
]


def make_users(client: TestClient, learners: int = 5) -> tuple[int, list[int]]:
    """Create one supervisor (id 1) and N learners; returns their ids."""
    r = client.post("/users", json={"name": "Alice", "role": "supervisor"})
    assert r.status_code == 201, r.text
    supervisor_id = r.json()["id"]
    learner_ids = []
    for i in range(learners):
        r = client.post(
            "/users", json={"name": f"Learner {i}", "role": "learner"}
        )
        assert r.status_code == 201, r.text
        learner_ids.append(r.json()["id"])
    return supervisor_id, learner_ids


def auth(uid: int) -> dict:
    return {"X-User-Id": str(uid)}


def linear_steps(n: int = 3) -> list[dict]:
    steps = [
        {
            "key": f"s{i}",
            "position": i,
            "instruction": f"do step {i}",
            "pass_condition": f"condition {i}",
            "prerequisite_keys": [f"s{i - 1}"] if i > 1 else [],
        }
        for i in range(1, n + 1)
    ]
    return steps


def create_published_program(
    client: TestClient,
    supervisor_id: int,
    steps: list[dict] | None = None,
    title: str = "P",
) -> dict:
    r = client.post(
        "/programs",
        headers=auth(supervisor_id),
        json={"title": title, "description": ""},
    )
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = client.post(
        f"/programs/{pid}/versions",
        headers=auth(supervisor_id),
        json={"steps": steps or linear_steps(3), "publish": True},
    )
    assert r.status_code == 201, r.text
    return {"program_id": pid, "version": r.json()}


def future_deadline(days: int = 7) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _app_now_iso(client, supervisor_id: int | None = None) -> str:
    """Current application clock (via service, no auth needed in test)."""
    from app.clock import now
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        return now(db).isoformat()
    finally:
        db.close()


def create_course(
    client: TestClient,
    supervisor_id: int,
    version_id: int,
    capacity: int = 1,
    deadline_days: int = 7,
) -> dict:
    # Deadline is computed against the (possibly frozen) application clock
    # so freeze-based expiry tests remain internally consistent.
    from datetime import datetime

    base = datetime.fromisoformat(_app_now_iso(client))
    deadline = (base + timedelta(days=deadline_days)).isoformat()
    r = client.post(
        "/courses",
        headers=auth(supervisor_id),
        json={
            "title": "C",
            "version_id": version_id,
            "capacity": capacity,
            "enroll_deadline": deadline,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def complete_as(
    client: TestClient,
    supervisor_id: int,
    learner_id: int,
    enrollment_id: int,
    step_keys: list[str],
) -> None:
    """Submit + pass every step as the learner/supervisor pair."""
    for key in step_keys:
        r = client.post(
            f"/enrollments/{enrollment_id}/steps/{key}/submit",
            headers=auth(learner_id),
            json={"content": f"work for {key}"},
        )
        assert r.status_code == 200, r.text
        r = client.post(
            f"/enrollments/{enrollment_id}/steps/{key}/review",
            headers=auth(supervisor_id),
            json={"passed": True},
        )
        assert r.status_code == 200, r.text
