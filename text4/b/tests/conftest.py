"""Pytest fixtures.

Tests run against a real PostgreSQL database (concurrency primitives such as
``SELECT ... FOR UPDATE`` and partial unique indexes are PostgreSQL-specific).
Target URL defaults to the local docker-compose role; override with
``TEST_DATABASE_URL``.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import app.database as database
from app.clock import reset_clock, set_clock
from app.database import Base
import app.models  # noqa: F401  (populate metadata)
from app.main import app

TEST_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://skillpulse:skillpulse@localhost:5432/skillpulse_test",
)

BASE_TIME = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(TEST_URL, poolclass=NullPool, future=True)
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db_engine(engine):
    # TRUNCATE keeps the schema but wipes every table between tests; RESTART
    # IDENTITY resets serial columns so IDs are predictable.
    with engine.connect() as conn:
        conn.execute(
            text(
                "TRUNCATE TABLE certificates, result_corrections, step_results, "
                "enrollments, step_prerequisites, steps, program_versions, "
                "programs, auth_sessions, users RESTART IDENTITY CASCADE"
            )
        )
        conn.commit()
    yield engine


@pytest.fixture()
def session_factory(db_engine):
    """Independent session factory (each session commits for real).

    Used by concurrency tests that need several sessions/threads at once.
    """
    return sessionmaker(bind=db_engine, expire_on_commit=False, future=True)


@pytest.fixture()
def client(db_engine) -> Iterator[TestClient]:
    # Request-scoped sessions bind to the same engine; each test truncates first.
    database.engine = db_engine
    database.SessionLocal.configure(bind=db_engine)
    # Freeze at BASE_TIME by default so deadlines/holds are deterministic;
    # tests using ``frozen_now`` re-freeze explicitly and can advance.
    set_clock(BASE_TIME)
    with TestClient(app) as c:
        yield c
    reset_clock()


@pytest.fixture()
def frozen_now():
    """Pin the application clock at BASE_TIME; returns the time and an advance().

    ``advance`` moves the clock *forward relative to the current time*, so
    successive calls accumulate (as real time does).
    """
    set_clock(BASE_TIME)
    state = {"now": BASE_TIME}

    def advance(hours: float = 0, days: float = 0, minutes: float = 0) -> datetime:
        state["now"] = state["now"] + timedelta(hours=hours, days=days, minutes=minutes)
        set_clock(state["now"])
        return state["now"]

    return BASE_TIME, advance


# ---------- API helpers ----------

def register(client: TestClient, login: str, role: str, *, password: str = "secret123",
             name: str | None = None) -> dict:
    response = client.post(
        "/api/auth/register",
        json={
            "name": name or login.title(),
            "login": login,
            "password": password,
            "role": role,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def make_user(client):
    created = {}

    def _make(login: str, role: str = "learner", **kwargs) -> dict:
        body = register(client, login, role, **kwargs)
        created[login] = body
        return body

    return _make


@pytest.fixture()
def supervisor(make_user):
    return make_user("sup", "supervisor")


@pytest.fixture()
def make_program(client, supervisor):
    def _make(title: str = "Safety", capacity: int = 3,
              deadline_days: int = 14, steps: int | None = None,
              publish: bool = True) -> dict:
        headers = auth_headers(supervisor["token"])
        body = {
            "title": title,
            "description": "demo",
            "capacity": capacity,
            "enrollment_deadline": (BASE_TIME + timedelta(days=deadline_days)).isoformat(),
        }
        resp = client.post("/api/programs", json=body, headers=headers)
        assert resp.status_code == 201, resp.text
        program = resp.json()

        versions = client.get(
            f"/api/programs/{program['id']}/versions", headers=headers
        ).json()
        draft = versions[0]

        if steps is not None:
            if steps == 0:
                step_payload = _default_steps(1)
            else:
                step_payload = _default_steps(steps)
        else:
            step_payload = _default_steps(3)
        if steps == 0:
            step_payload[0]["title"] = "Only step"
        resp = client.put(
            f"/api/programs/versions/{draft['id']}/steps",
            json={"steps": step_payload},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text

        if publish:
            resp = client.post(
                f"/api/programs/versions/{draft['id']}/publish", headers=headers
            )
            assert resp.status_code == 200, resp.text
        program["draft_id"] = draft["id"]
        versions = client.get(
            f"/api/programs/{program['id']}/versions", headers=headers
        ).json()
        published = [v for v in versions if v["status"] == "published"]
        program["version_id"] = published[0]["id"] if published else None
        program["steps"] = published[0]["steps"] if published else []
        return program

    return _make


def _default_steps(n: int) -> list[dict]:
    steps = []
    for i in range(1, n + 1):
        prereqs = [i - 1] if i > 1 else []
        steps.append(
            {
                "title": f"Step {i}",
                "instruction": f"Do step {i}",
                "pass_condition": f"Pass condition {i}",
                "prerequisite_positions": prereqs,
            }
        )
    return steps


@pytest.fixture()
def enrolled_confirmed(client, make_program, make_user):
    """Return a helper creating a learner with a CONFIRMED seat and results API."""

    def _make(program: dict, login: str) -> dict:
        learner = make_user(login, "learner")
        headers = auth_headers(learner["token"])
        resp = client.post(f"/api/programs/{program['id']}/enroll", headers=headers)
        assert resp.status_code == 201, resp.text
        enrollment = resp.json()
        resp = client.post(f"/api/enrollments/{enrollment['id']}/confirm", headers=headers)
        assert resp.status_code == 200, resp.text
        return {"user": learner, "headers": headers, "enrollment": resp.json()}

    return _make
