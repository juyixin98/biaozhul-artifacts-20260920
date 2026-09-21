import os

os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://admin:admin@127.0.0.1:5432/skillpulse_test"
)

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app import clock
from app.config import DATABASE_URL
from app.db import engine
from app.main import app
from app.models import Base


def _ensure_database() -> None:
    url = make_url(DATABASE_URL)
    maint = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with maint.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": url.database}
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    maint.dispose()


@pytest.fixture(scope="session", autouse=True)
def schema():
    _ensure_database()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def clean_tables(schema):
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    clock.reset_clock()
    yield
    clock.reset_clock()


class FakeClock:
    """Controllable clock for expiry tests."""

    def __init__(self, start: datetime | None = None):
        self.t = start or datetime.now(timezone.utc)

    def now(self) -> datetime:
        return self.t

    def advance(self, **kwargs) -> None:
        self.t += timedelta(**kwargs)


@pytest.fixture
def fake_clock():
    fc = FakeClock()
    clock.set_clock(fc.now)
    yield fc
    clock.reset_clock()


@pytest.fixture
def client():
    return TestClient(app)
