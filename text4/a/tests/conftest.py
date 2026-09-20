import os

import pytest
from sqlalchemy import text

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://skillpulse:skillpulse@localhost:5432/skillpulse_test",
)
os.environ["ALLOW_CLOCK_CONTROL"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app import models  # noqa: E402
from app.database import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402

# Fresh schema for the whole test session.
models.Base.metadata.drop_all(bind=engine)
models.Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def clean_db():
    db = SessionLocal()
    try:
        # CASCADE handles the programs <-> program_versions FK cycle.
        names = ", ".join(t.name for t in models.Base.metadata.sorted_tables)
        db.execute(text(f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE"))
        db.commit()
    finally:
        db.close()
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
