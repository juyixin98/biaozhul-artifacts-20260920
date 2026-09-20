"""Helpers shared across tests."""
from __future__ import annotations

import time
import uuid

from sqlalchemy import select

from app import queue as q
from app.config import get_settings
from app.db import SessionLocal
from app.models import Job, JobStatus
from app.runner import run_job


def run_to_end(job_id: int, timeout: float = 60.0) -> str:
    """Claim a queued job and execute it synchronously in this thread."""
    executor = f"test-exec-{uuid.uuid4().hex[:8]}"
    settings = get_settings()
    with SessionLocal() as db:
        job = q.claim_job(db, executor_id=executor, settings=settings)
        assert job is not None and job.id == job_id
    with SessionLocal() as run_db:
        job = run_db.get(Job, job_id)
        status = run_job(run_db, job, settings=settings)
    return status


def wait_for_status(job_id: int, expected: set[str], timeout: float = 60.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with SessionLocal() as db:
            s = db.get(Job, job_id).status.value
        if s in expected:
            return s
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not reach {expected} within {timeout}s")


def get_job(job_id: int) -> Job:
    with SessionLocal() as db:
        return db.get(Job, job_id)


def list_job_events(job_id: int) -> list[dict]:
    from app.models import Event
    with SessionLocal() as db:
        rows = db.scalars(
            select(Event).where(Event.job_id == job_id).order_by(Event.seq)
        ).all()
        return [{"seq": e.seq, "kind": e.kind, "payload": e.payload} for e in rows]
