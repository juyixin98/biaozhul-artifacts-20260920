"""Alembic migrations upgrade and downgrade cleanly on a scratch database."""

import os
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url


@pytest.fixture(scope="module")
def scratch_database():
    from app.config import settings

    url = make_url(settings.database_url)
    scratch_name = f"cloudgate_mig_{uuid.uuid4().hex[:8]}"
    admin = create_engine(
        url.set(database="postgres").render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )
    with admin.connect() as conn:
        conn.execute(text(f'create database "{scratch_name}"'))
    admin.dispose()

    scratch_url = url.set(database=scratch_name).render_as_string(hide_password=False)
    yield scratch_url

    admin = create_engine(
        url.set(database="postgres").render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )
    with admin.connect() as conn:
        conn.execute(text(f'drop database "{scratch_name}" with (force)'))
    admin.dispose()


def _alembic_config(scratch_url: str):
    from alembic.config import Config

    config = Config("alembic.ini")
    config.set_main_option("script_location", "migrations")
    config.set_main_option("sqlalchemy.url", scratch_url)
    # env.py only overrides its URL when CLOUDGATE_ALEMBIC_URL is exported.
    os.environ["CLOUDGATE_ALEMBIC_URL"] = scratch_url
    return config


EXPECTED_TABLES = {
    "tenants",
    "admin_users",
    "address_pools",
    "pool_addresses",
    "access_points",
    "devices",
    "sessions",
    "lease_terminations",
    "alembic_version",
}


def test_upgrade_creates_schema_then_downgrade_drops_it(scratch_database):
    from alembic import command

    config = _alembic_config(scratch_database)
    command.upgrade(config, "head")

    engine = create_engine(scratch_database)
    try:
        tables = set(inspect(engine).get_table_names())
        assert EXPECTED_TABLES <= tables

        with engine.connect() as conn:
            # Partial unique indexes enforcing the core invariants exist.
            indexes = {row[0] for row in conn.execute(text(
                "select indexname from pg_indexes where tablename in ('sessions','pool_addresses')"
            ))}
        assert "uq_active_session_per_device" in indexes
        assert "uq_pool_allocated_address" in indexes
    finally:
        engine.dispose()

    command.downgrade(config, "base")
    engine = create_engine(scratch_database)
    try:
        tables = set(inspect(engine).get_table_names())
        assert tables == {"alembic_version"}
    finally:
        engine.dispose()
