"""Shared pytest fixtures."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from kex.app import create_app
from kex.config import Config
from kex.db import configure, get_engine, session_scope
from kex.models import Base, JobItem


@pytest.fixture()
def cfg(tmp_path):
    db_path = str(tmp_path / "test.db")
    c = Config(
        db_path=db_path,
        echo_sql=False,
        max_workers=2,
        embedded_worker=False,
        worker_poll_interval=0.05,
        worker_lease_seconds=2,
        worker_heartbeat_seconds=1,
    )
    configure(c)
    engine = get_engine()
    Base.metadata.create_all(engine)
    yield c


@pytest.fixture()
def app(cfg):
    application = create_app(cfg, start_worker=False)
    application.testing = True
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()


def create_ws(client, name: str) -> tuple[int, str]:
    resp = client.post("/api/workspaces", json={"name": name})
    assert resp.status_code == 201, resp.data
    body = resp.get_json()
    return body["workspace_id"], body["api_key"]


def auth(ws_id: int, key: str) -> dict[str, str]:
    return {"X-Workspace-Key": key}


def drain(worker_id: str = "drainer", lease: int = 60, expect_failed: bool = False):
    """Process every queued item synchronously (checkpoint-aware loop)."""
    from kex.services import jobs as jobs_service

    processed = []
    while True:
        with session_scope() as db:
            claimed = jobs_service.claim_item(db, worker_id, lease)
            if claimed is None:
                break
            item_id, ws_id = claimed.item.id, claimed.item.workspace_id
            status = jobs_service.process_item(db, claimed)
            jobs_service.maybe_finalize_job(db, claimed.job.id)
            jobs_service._activate_any_ready_building(db, ws_id)
        processed.append((item_id, status))

    # Final sweep in case ordering left a building generation pending.
    with session_scope() as db:
        from kex.models import Workspace
        for ws in db.query(Workspace).all():
            jobs_service._activate_any_ready_building(db, ws.id)
    return processed


def pending_count() -> int:
    from sqlalchemy import select

    with session_scope() as db:
        return len(
            db.execute(
                select(JobItem.id).where(
                    JobItem.status.in_(["queued", "running"])
                )
            ).all()
        )


def wait_drained(timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pending_count() == 0:
            return
        time.sleep(0.05)
    raise AssertionError("jobs did not drain in time")


class ThreadedWorkers:
    """Run up to two real worker threads against the configured database."""

    def __init__(self, cfg, n: int = 2):
        from kex.services.jobs import Worker

        assert n <= 2
        self.cfg = cfg
        self.workers = [
            Worker(
                poll_interval=cfg.worker_poll_interval,
                lease_seconds=cfg.worker_lease_seconds,
                heartbeat_seconds=cfg.worker_heartbeat_seconds,
                max_workers=cfg.max_workers,
                worker_id=f"thread-w{i}",
            )
            for i in range(n)
        ]
        self.threads: list[threading.Thread] = []

    def start(self):
        for w in self.workers:
            self.threads.append(w.start_in_thread())
        return self

    def stop(self):
        from kex.db import write_session
        from kex.services.jobs import deregister

        for w in self.workers:
            w.stop()
        for t in self.threads:
            t.join(timeout=5)
        # Remove heartbeats so a still-shutting-down thread / a later test
        # cannot observe stale registrations and wrongly hit the 2-worker cap.
        for w in self.workers:
            try:
                with write_session() as db:
                    deregister(db, w.worker_id)
            except Exception:
                pass


@pytest.fixture()
def threaded_workers(cfg):
    started: list[ThreadedWorkers] = []

    def _start(n: int = 2):
        tw = ThreadedWorkers(cfg, n).start()
        started.append(tw)
        return tw

    yield _start
    for tw in started:
        tw.stop()


@pytest.fixture()
def migrated_db_path(tmp_path):
    """Exercise the real Alembic upgrade path in a subprocess."""
    db_path = str(tmp_path / "migrated.db")
    env = {**os.environ, "KEX_DB_PATH": db_path, "PYTHONPATH": os.getcwd()}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True, env=env, capture_output=True,
    )
    return db_path
