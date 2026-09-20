"""Pytest setup.

Every test gets its own freshly migrated database, so tests never observe each
other's data.  The schema is created via the application's Alembic revision,
which also exercises the migration script.  The escalation worker is disabled
(``ENABLE_WORKER=false``); timeout behaviour is tested by calling the engine
function directly.
"""
from __future__ import annotations

import os
import time
import uuid

import pytest_asyncio
from sqlalchemy import text

# Configure environment BEFORE importing application modules.
_ADMIN_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://workflow:workflow@localhost:5432/postgres",
)
os.environ["ENABLE_WORKER"] = "false"

from sqlalchemy.ext.asyncio import (  # noqa: E402
    async_sessionmaker,
    create_async_engine,
)

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402


@pytest_asyncio.fixture
async def db_engine():
    db_name = f"workflow_test_{uuid.uuid4().hex[:12]}"
    test_url = _ADMIN_URL.rsplit("/", 1)[0] + "/" + db_name

    admin_engine = create_async_engine(_ADMIN_URL, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as conn:
        await conn.exec_driver_sql(f'CREATE DATABASE "{db_name}"')
    await admin_engine.dispose()

    # point application settings/config at the per-test database
    os.environ["DATABASE_URL"] = test_url
    from app.config import get_settings
    get_settings.cache_clear()

    engine = create_async_engine(test_url)
    old_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = test_url
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")

    yield engine

    await engine.dispose()
    cleanup_engine = create_async_engine(_ADMIN_URL, isolation_level="AUTOCOMMIT")
    last_exc = None
    async with cleanup_engine.connect() as conn:
        # Repeatedly terminate lingering backends held by the SAME role
        # (a non-superuser cannot terminate superuser/auxiliary backends such
        # as autovacuum -- those leave on their own) and retry the drop,
        # because backends take a few ms to actually exit.
        for _ in range(50):
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :db AND usename = current_user "
                    "AND pid <> pg_backend_pid()"
                ),
                {"db": db_name},
            )
            try:
                await conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{db_name}"')
                last_exc = None
                break
            except Exception as exc:  # backend still shutting down
                last_exc = exc
                time.sleep(0.2)
        if last_exc is not None:
            raise last_exc
    await cleanup_engine.dispose()
    if old_url is not None:
        os.environ["DATABASE_URL"] = old_url
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def session_factory(db_engine):
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def client(db_engine):
    from app.db import get_session
    from app.main import app

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def _override_get_session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
