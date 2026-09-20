from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import AdminUser
from app.security import hash_password

ROOT = Path(__file__).resolve().parents[1]

TEST_DB_URL = os.environ.get(
    "CLOUDGATE_TEST_DATABASE_URL",
    "postgresql+psycopg2:///cloudgate_t12b_test",
)

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "test-admin-pw"


@pytest.fixture(scope="session")
def db_engine():
    """Fresh schema, migrated with alembic, once per test run."""
    os.environ["CLOUDGATE_DATABASE_URL"] = TEST_DB_URL
    admin_engine = create_engine(TEST_DB_URL, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    admin_engine.dispose()

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", TEST_DB_URL)
    command.upgrade(cfg, "head")

    engine = create_engine(TEST_DB_URL, pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean_tables(db_engine):
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE leases, devices, access_points, ipv4_pools, "
                "admin_users, tenants RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        database_url=TEST_DB_URL,
        jwt_secret="test-secret",
        jwt_expiry_seconds=3600,
        heartbeat_interval_seconds=60,
        lease_ttl_seconds=600,
        sweep_interval_seconds=0,  # no background sweeper in tests; sweep explicitly
        admin_username=ADMIN_USERNAME,
        admin_password=ADMIN_PASSWORD,
    )


@pytest.fixture()
def app(settings):
    application = create_app(settings)
    # Seed the global admin directly (lifespan does not run without `with TestClient`).
    with application.state.SessionLocal() as db:
        db.add(
            AdminUser(
                username=ADMIN_USERNAME,
                password_hash=hash_password(ADMIN_PASSWORD),
                tenant_id=None,
            )
        )
        db.commit()
    return application


@pytest.fixture()
def client(app):
    return TestClient(app)


@pytest.fixture()
def db(db_engine):
    with Session(db_engine) as session:
        yield session
