"""Job queue: claim-with-lease, fencing, transitions, events, checkpoints.

Concurrency model (PostgreSQL):

* ``claim_job`` serialises all claims with one transaction-scoped advisory
  lock, then scans candidate rows taken ``FOR UPDATE SKIP LOCKED`` and
  re-checks each owner's *valid* running-job count under
  ``max_running_per_user`` (skipping users that are capped). Two executors
  can therefore never both slip past the same user's cap.
* Every later mutation by an executor (heartbeat, event, checkpoint, epoch
  commit) carries its ``executor_id`` fencing token. A lease that expired and
  was re-queued/claimed by someone else silently invalidates every write
  from the old executor (0 rows updated → ``LeaseLost``).
* Per-job event ``seq`` is gapless: appending locks the job row first.
"""
from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .checkpoints import KEEP_LAST_N, is_valid_file, prune_paths
from .config import Settings, get_settings
from .graph import output_dim
from .models import (
    Architecture,
    CheckpointRef,
    Dataset,
    Event,
    Job,
    JobStatus,
    utcnow,
)


class LeaseLost(RuntimeError):
    """Raised when an executor's lease is gone or owned by another executor."""


class QueueError(ValueError):
    pass


DEFAULT_HYPERPARAMS: dict[str, float] = {
    "lr": 0.01,
    "batch_size": 32,
    "val_fraction": 0.2,
    "weight_decay": 0.0,
}


def validate_hyperparams(params: dict[str, Any]) -> dict[str, Any]:
    merged = {**DEFAULT_HYPERPARAMS, **(params or {})}
    if not isinstance(params or {}, dict):
        raise QueueError("hyperparams must be an object")
    lr = merged["lr"]
    if not (isinstance(lr, (int, float)) and not isinstance(lr, bool)) or not (
        lr > 0
    ):
        raise QueueError("hyperparams.lr must be a positive number")
    wd = merged["weight_decay"]
    if not (isinstance(wd, (int, float)) and not isinstance(wd, bool)) or wd < 0:
        raise QueueError("hyperparams.weight_decay must be >= 0")
    bs = merged["batch_size"]
    if not isinstance(bs, int) or isinstance(bs, bool) or bs < 1:
        raise QueueError("hyperparams.batch_size must be a positive integer")
    vf = merged["val_fraction"]
    if not (isinstance(vf, (int, float)) and not isinstance(vf, bool)):
        raise QueueError("hyperparams.val_fraction must be a number")
    if not (0.0 < float(vf) < 1.0):
        raise QueueError("hyperparams.val_fraction must be in (0, 1)")
    merged["lr"] = float(lr)
    merged["weight_decay"] = float(wd)
    merged["val_fraction"] = float(vf)
    return merged


# ---------------------------------------------------------------- jobs


def list_events(db: Session, job_id: int, after_seq: int = 0) -> list[Event]:
    return list(
        db.scalars(
            select(Event)
            .where(Event.job_id == job_id, Event.seq > after_seq)
            .order_by(Event.seq)
        )
    )


def create_job(
    db: Session,
    *,
    user_id: str,
    architecture_id: int,
    dataset_id: int,
    total_epochs: int,
    seed: int,
    hyperparams: dict[str, Any] | None,
    settings: Settings | None = None,
) -> Job:
    settings = settings or get_settings()
    if not isinstance(total_epochs, int) or isinstance(total_epochs, bool):
        raise QueueError("epochs must be an integer")
    if not 1 <= total_epochs <= 10_000:
        raise QueueError("epochs must be in [1, 10000]")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise QueueError("seed must be an integer")
    if not -(2**63) <= seed < 2**63:
        raise QueueError("seed out of range")

    arch = db.get(Architecture, architecture_id)
    if arch is None:
        raise QueueError("architecture not found")
    ds = db.get(Dataset, dataset_id)
    if ds is None:
        raise QueueError("dataset not found")

    # The 3-job-per-user limit applies to *running* jobs (jobs holding a
    # valid lease). Any number of jobs may sit in the queue.
    now = utcnow()
    running = db.scalar(
        select(func.count())
        .select_from(Job)
        .where(
            Job.user_id == user_id,
            Job.status == JobStatus.RUNNING,
            Job.lease_expires_at > now,
        )
    )
    if running >= settings.max_running_per_user:
        raise QueueError(
            f"user {user_id!r} already has {running} running jobs "
            f"(limit {settings.max_running_per_user}); wait or cancel one"
        )

    hp = validate_hyperparams(hyperparams)
    # Output shape sanity vs dataset happens here too: a classifier's final
    # layer width is informational; regression expects width 1.
    out_dim = output_dim(arch.spec)
    if ds.task == "regression" and out_dim != 1:
        raise QueueError(
            f"regression datasets require output width 1, architecture outputs {out_dim}"
        )

    # Fixed split indices, snapshot stored on the job row itself.
    from .datasets import make_split

    split = make_split(ds.num_rows, hp["val_fraction"], seed=seed + 17)

    job = Job(
        user_id=user_id,
        architecture_id=arch.id,
        dataset_id=ds.id,
        hyperparams=hp,
        seed=seed,
        dataset_digest=ds.digest,
        split=split,
        epochs_completed=0,
        total_epochs=total_epochs,
        status=JobStatus.QUEUED,
    )
    db.add(job)
    db.flush()
    _append_event(db, job, "status", {"from": None, "to": JobStatus.QUEUED.value})
    db.commit()
    db.refresh(job)
    return job


# ---------------------------------------------------------------- claim / lease


def claim_job(
    db: Session, *, executor_id: str, settings: Settings | None = None
) -> Job | None:
    """Atomically claim the next eligible queued job for this executor.

    A single transaction-scoped advisory lock serialises all claims. Claim
    throughput is low (a handful of worker threads), so a global lock is
    cheaper and deadlock-free compared to per-user locks; it also makes the
    per-user running cap exact under concurrency.
    """
    settings = settings or get_settings()
    now = utcnow()
    lease = timedelta(seconds=settings.leader_lease_seconds)

    db.execute(_advisory_lock("nnlab:claim"))
    candidates = list(
        db.scalars(
            select(Job)
            .where(Job.status == JobStatus.QUEUED)
            .order_by(Job.created_at, Job.id)
            .limit(50)
            .with_for_update(skip_locked=True)
        )
    )
    for job in candidates:
        valid_running = db.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.user_id == job.user_id,
                Job.status == JobStatus.RUNNING,
                Job.lease_expires_at > now,
            )
        )
        if valid_running >= settings.max_running_per_user:
            # This user is capped; try the next candidate (other users).
            continue
        job.status = JobStatus.RUNNING
        job.executor_id = executor_id
        job.lease_expires_at = now + lease
        _append_event(db, job, "status",
                      {"from": JobStatus.QUEUED.value, "to": JobStatus.RUNNING.value})
        db.commit()
        db.refresh(job)
        return job
    # Release the advisory lock promptly.
    db.rollback()
    return None


def heartbeat(db: Session, job_id: int, executor_id: str) -> Job:
    now = utcnow()
    settings = get_settings()
    result = db.query(Job).filter(
        Job.id == job_id,
        Job.executor_id == executor_id,
        Job.status == JobStatus.RUNNING,
    ).update({"lease_expires_at": now + timedelta(seconds=settings.leader_lease_seconds)})
    db.commit()
    if result != 1:
        raise LeaseLost("heartbeat rejected: lease lost or job no longer running")
    db.refresh(job)
    return job


def reap_expired_leases(db: Session) -> int:
    """Return running jobs whose lease expired to the queue.

    The old executor is fenced: executor_id/lease are cleared and the job
    becomes QUEUED so another executor can resume from the last checkpoint.
    """
    now = utcnow()
    jobs = list(
        db.scalars(
            select(Job)
            .where(Job.status == JobStatus.RUNNING, Job.lease_expires_at <= now)
            .with_for_update(skip_locked=True)
        )
    )
    for job in jobs:
        old = job.executor_id
        job.status = JobStatus.QUEUED
        job.executor_id = None
        job.lease_expires_at = None
        _append_event(
            db,
            job,
            "log",
            {"message": f"lease of executor {old!r} expired; job re-queued"},
        )
    if jobs:
        db.commit()
    return len(jobs)


# ---------------------------------------------------------------- user controls


def pause_job(db: Session, job_id: int, user_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None or job.user_id != user_id:
        raise QueueError("job not found")
    if job.status in (JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED):
        raise QueueError(f"cannot pause a {job.status.value} job")
    if job.status == JobStatus.PAUSED:
        return job
    _locked_transition(db, job, JobStatus.PAUSED, clear_executor=False)
    return job


def resume_job(db: Session, job_id: int, user_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None or job.user_id != user_id:
        raise QueueError("job not found")
    if job.status != JobStatus.PAUSED:
        raise QueueError(f"only paused jobs can be resumed (is {job.status.value})")
    # Clear the old executor: its token must never write to this job again.
    job.executor_id = None
    job.lease_expires_at = None
    _locked_transition(db, job, JobStatus.QUEUED, clear_executor=False)
    return job


def cancel_job(db: Session, job_id: int, user_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None or job.user_id != user_id:
        raise QueueError("job not found")
    if job.status in (JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED):
        raise QueueError(f"cannot cancel a {job.status.value} job")
    previous = job.status
    job.status = JobStatus.CANCELLED
    job.executor_id = None
    job.lease_expires_at = None
    _append_event(db, job, "status",
                  {"from": previous.value, "to": JobStatus.CANCELLED.value})
    db.commit()
    db.refresh(job)
    return job


def _locked_transition(db: Session, job: Job, new_status: JobStatus, *,
                       clear_executor: bool) -> None:
    # Lock the row so a concurrent executor epoch-commit observes the request.
    locked = db.scalar(select(Job).where(Job.id == job.id).with_for_update())
    previous = locked.status
    if previous in (JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED):
        db.rollback()
        raise QueueError(f"job already {previous.value}")
    locked.status = new_status
    if clear_executor:
        locked.executor_id = None
        locked.lease_expires_at = None
    _append_event(db, locked, "status",
                  {"from": previous.value, "to": new_status.value})
    db.commit()
    db.refresh(job)


# ---------------------------------------------------------------- events / checkpoints


def _advisory_lock(key: str):
    from sqlalchemy import text

    return text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))").bindparams(k=key)


def _append_event(
    db: Session, job: Job, kind: str, payload: dict[str, Any]
) -> Event:
    """Append an event with the next gapless seq. Caller holds the tx; the
    job row is locked to serialise concurrent appenders."""
    locked = db.scalar(select(Job.id).where(Job.id == job.id).with_for_update())
    if locked is None:
        raise QueueError("job vanished")
    max_seq = db.scalar(
        select(func.coalesce(func.max(Event.seq), 0)).where(Event.job_id == job.id)
    )
    event = Event(job_id=job.id, seq=max_seq + 1, kind=kind, payload=payload)
    db.add(event)
    db.flush()
    return event


def append_event_fenced(
    db: Session, job_id: int, executor_id: str, kind: str, payload: dict[str, Any]
) -> Event:
    job = db.get(Job, job_id)
    if job is None or job.executor_id != executor_id:
        raise LeaseLost("event append rejected: executor fence failed")
    event = _append_event(db, job, kind, payload)
    db.commit()
    return event


def commit_epoch(
    db: Session,
    *,
    job_id: int,
    executor_id: str,
    epoch: int,
    metrics: dict[str, float],
    checkpoint_path: str,
    checkpoint_size: int,
) -> str:
    """Publish an epoch boundary under the executor fencing token.

    Checkpoint file must already be atomically on disk. Returns the resulting
    job status: running | paused | cancelled | completed.
    """
    job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if job is None:
        raise QueueError("job not found")
    if job.executor_id != executor_id:
        db.rollback()
        raise LeaseLost("epoch commit rejected: executor fence failed")

    events: list[tuple[str, dict]] = [
        ("metrics", {"epoch": epoch, **metrics}),
    ]

    # Publish the checkpoint reference only after the atomic file landed
    # (caller guarantee) and validate it is readable here.
    if not is_valid_file(checkpoint_path, expected_epoch=epoch):
        db.rollback()
        raise QueueError(f"refusing to publish corrupt checkpoint for epoch {epoch}")

    # Upsert the checkpoint reference by (job, epoch). A retried epoch is
    # normal after falling back to an earlier checkpoint (the retrained
    # epoch's atomic file overwrote the same path); only a duplicate commit
    # from the SAME executor at an epoch the job already moved past is an
    # error, and the fence/status checks above already cover that.
    ref = db.scalar(
        select(CheckpointRef).where(
            CheckpointRef.job_id == job.id, CheckpointRef.epoch == epoch
        ).with_for_update()
    )
    if ref is None:
        ref = CheckpointRef(
            job_id=job.id, epoch=epoch, path=checkpoint_path,
            size_bytes=checkpoint_size, valid=True,
        )
        db.add(ref)
    else:
        ref.path = checkpoint_path
        ref.size_bytes = checkpoint_size
        ref.valid = True
    db.flush()

    job.epochs_completed = epoch

    requested = job.status
    if requested == JobStatus.CANCELLED:
        final = JobStatus.CANCELLED
        job.executor_id = None
        job.lease_expires_at = None
    elif requested == JobStatus.PAUSED:
        final = JobStatus.PAUSED
        # Lease stays with this executor until it unwinds; drop it on exit so
        # the job can only move via explicit resume.
        job.executor_id = None
        job.lease_expires_at = None
    elif epoch >= job.total_epochs:
        final = JobStatus.COMPLETED
        job.status = JobStatus.COMPLETED
        job.executor_id = None
        job.lease_expires_at = None
    else:
        final = JobStatus.RUNNING

    if final != JobStatus.RUNNING:
        events.append(("status", {"from": JobStatus.RUNNING.value, "to": final.value}))

    for kind, payload in events:
        _append_event(db, job, kind, payload)

    # Prune checkpoint refs to the newest KEEP_LAST_N *valid* ones; invalid
    # (corrupt) refs are retained for audit. Query fresh rows from the DB so
    # the just-upserted ref is counted exactly once. Files are removed after
    # commit so a rolled-back publication never loses its file.
    db.flush()
    all_valid = list(
        db.scalars(
            select(CheckpointRef)
            .where(CheckpointRef.job_id == job.id, CheckpointRef.valid.is_(True))
            .order_by(CheckpointRef.epoch)
        )
    )
    stale = prune_paths(all_valid, keep=KEEP_LAST_N)
    stale_files = [r.path for r in stale]
    for r in stale:
        db.delete(r)
    db.commit()
    for path in stale_files:
        try:
            os.remove(path)
        except OSError:
            pass
    return final.value


def mark_failed(
    db: Session, *, job_id: int, executor_id: str, error: str
) -> None:
    job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if job is None:
        return
    if job.executor_id != executor_id:
        db.rollback()
        raise LeaseLost("failure report rejected: executor fence failed")
    job.status = JobStatus.FAILED
    job.error = error[:4000]
    job.executor_id = None
    job.lease_expires_at = None
    _append_event(db, job, "status",
                  {"from": JobStatus.RUNNING.value, "to": JobStatus.FAILED.value})
    db.commit()
