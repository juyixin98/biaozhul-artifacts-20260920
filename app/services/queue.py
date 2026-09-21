"""Training queue: bounded concurrency per user and lease-based claiming."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import LEASE_SECONDS, MAX_RUNNING_PER_USER
from ..models.orm import Job, JobStatus, utcnow


class ClaimError(Exception):
    pass


def count_running_for_user(db: Session, user_id: str) -> int:
    """Jobs currently consuming one of the user's 3 slots."""
    return int(
        db.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.user_id == user_id,
                Job.status == JobStatus.RUNNING,
            )
        )
    )


def requeue_expired_leases(db: Session) -> int:
    """RUNNING jobs whose lease lapsed return to QUEUED so another executor
    picks them up. The stale executor keeps its (now expired) executor_id and
    is rejected by every guarded write (see ``owns_active_lease``).
    """
    now = utcnow()
    stale = list(
        db.scalars(
            select(Job).where(
                Job.status == JobStatus.RUNNING,
                Job.lease_expires_at.is_not(None),
                Job.lease_expires_at < now,
            )
        )
    )
    for job in stale:
        job.status = JobStatus.QUEUED
        job.executor_id = None
        job.lease_expires_at = None
        job.heartbeat_at = None
    if stale:
        db.commit()
    return len(stale)


def claim_next_job(db: Session, executor_id: str) -> Job | None:
    """Atomically claim one queued job.

    Concurrency-safe: each candidate is locked individually with
    ``SELECT ... FOR UPDATE SKIP LOCKED`` in its own short transaction, so two
    workers never block on or receive the same row, and a worker skipping a
    user whose 3 slots are full does not hold locks that starve everyone else.
    """
    now = utcnow()
    # Listing does not take row locks; end that read transaction before the
    # per-candidate claim transactions below.
    candidate_ids = list(
        db.scalars(
            select(Job.id)
            .where(Job.status == JobStatus.QUEUED)
            .order_by(Job.created_at, Job.id)
            .limit(50)
        )
    )
    db.rollback()
    for job_id in candidate_ids:
        # One short transaction per candidate. ``skip_locked`` makes a row
        # held by another worker invisible instead of blocking us.
        job = db.scalar(
            select(Job)
            .where(Job.id == job_id)
            .with_for_update(skip_locked=True)
        )
        if job is None or job.status != JobStatus.QUEUED:
            db.rollback()  # locked elsewhere, or already claimed
            continue
        if count_running_for_user(db, job.user_id) >= MAX_RUNNING_PER_USER:
            db.rollback()  # user's slots full: release lock, try next job
            continue
        job.status = JobStatus.RUNNING
        job.executor_id = executor_id
        job.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
        job.heartbeat_at = now
        if job.started_at is None:
            job.started_at = now
        db.commit()
        return job
    return None


def heartbeat(db: Session, job_id: str, executor_id: str) -> bool:
    """Renew the lease while this executor still owns the row.

    A job that was flipped to PAUSED/CANCELLING at the control endpoint is
    *still owned* by this executor until it reaches the epoch boundary, so we
    keep its lease alive rather than treating the control flip as lease loss.
    Returns False only when ownership is gone (requeued, superseded, done).
    """
    job = db.get(Job, job_id)
    if job is None or job.executor_id != executor_id:
        return False
    if job.status in (JobStatus.QUEUED, JobStatus.COMPLETED,
                      JobStatus.CANCELLED, JobStatus.FAILED):
        return False
    now = utcnow()
    job.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
    job.heartbeat_at = now
    db.commit()
    return True


def owns_active_lease(db: Session, job_id: str, executor_id: str) -> bool:
    """Guard for metric/checkpoint submissions: an executor that lost its
    lease (or was superseded) must never write again.

    Note this deliberately does NOT inspect ``status``: a RUNNING job may
    legitimately have been flipped to PAUSED at the control endpoint while the
    current executor is finishing its epoch. Status is evaluated only after
    the epoch's metrics/checkpoint are durably published.
    """
    job = db.get(Job, job_id)
    if job is None:
        return False
    if job.executor_id != executor_id:
        return False
    if job.lease_expires_at is None or job.lease_expires_at <= utcnow():
        return False
    return True
