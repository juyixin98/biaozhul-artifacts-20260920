"""Test fixtures: real PostgreSQL, fresh schema, one client per test.

Database URL comes from ``WF_TEST_DATABASE_URL`` and falls back to the local
development database. All tables are created once; per-test cleanup truncates
everything so ids/sequences never leak between tests.
"""
from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

os.environ.setdefault("WF_SCHEDULER_ENABLED", "false")
os.environ.setdefault(
    "WF_TEST_DATABASE_URL",
    "postgresql+psycopg2://wfuser:wfpass@127.0.0.1:5432/wfdb",
)
os.environ["WF_DATABASE_URL"] = os.environ["WF_TEST_DATABASE_URL"]


@pytest.fixture(scope="session")
def engine():
    from app.db import Base
    import app.models  # noqa: F401
    from app.db import engine as eng

    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    from sqlalchemy.orm import Session

    session = Session(engine)
    yield session
    session.close()
    # clean every business table before the next test
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE scheduled_escalations, history_records, approval_requests, "
                "tasks, instances, template_versions, templates RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def client(engine, db):
    """Depend on ``db`` so the per-test TRUNCATE always runs, even for tests
    that only use the HTTP client."""
    from app.main import app

    with TestClient(app) as c:
        yield c


def new_request_id() -> str:
    return f"req-{uuid.uuid4()}"


@pytest.fixture
def req_id():
    return new_request_id
