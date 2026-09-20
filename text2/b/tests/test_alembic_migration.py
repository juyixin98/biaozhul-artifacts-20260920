"""Verify the Alembic migration builds a schema equivalent to the ORM metadata.

This test creates a *separate* scratch database, applies ``alembic upgrade
head`` against it, and checks that the critical constraints exist (including
the partial unique indexes that protect invitation consistency).
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect, text

pytestmark = pytest.mark.migration


def _scratch_url() -> str:
    base = os.environ["TEST_DATABASE_URL"]
    head, _ = base.rsplit("/", 1)
    return head + "/careforce_mig_test"


def test_alembic_upgrade_creates_schema(monkeypatch):
    url = _scratch_url()
    head, db_name = url.rsplit("/", 1)
    admin = create_engine(head + "/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    admin.dispose()

    monkeypatch.setenv("DATABASE_URL", url)
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")

    engine = create_engine(url)
    try:
        insp = inspect(engine)
        tables = set(insp.get_table_names())
        expected = {
            "units",
            "coordinators",
            "workers",
            "qualifications",
            "worker_qualifications",
            "care_plans",
            "plan_versions",
            "weekly_slots",
            "plan_qualifications",
            "plan_prerequisites",
            "tasks",
            "task_qualifications",
            "task_prerequisites",
            "assignments",
            "assignment_events",
            "coordinator_units",
            "worker_units",
        }
        assert expected <= tables

        indexes = {ix["name"]: ix for ix in insp.get_indexes("assignments")}
        assert "uq_one_pending_per_task" in indexes
        assert "uq_one_accepted_per_task" in indexes
        assert indexes["uq_one_pending_per_task"]["unique"] is True

        # Idempotency: a second upgrade is a no-op.
        command.upgrade(cfg, "head")
    finally:
        engine.dispose()
        admin = create_engine(head + "/postgres", isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": db_name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        admin.dispose()
