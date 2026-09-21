"""Job executor: claim -> (resume checkpoint) -> train epoch-by-epoch.

All writes from this runner are gated by ``owns_active_lease``: if the lease
expired (worker stall) or was superseded, metrics and checkpoints are refused
— a stale executor can never contaminate the event stream or checkpoint store.
"""
from __future__ import annotations

import threading

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models.network_spec import parse_spec
from ..models.orm import Job, JobStatus, utcnow
from ..services import data_safe, events, queue
from ..services.checkpoints import (
    latest_valid_checkpoint,
    load_state,
    save_checkpoint,
)
from ..services.training import Engine, HyperParams, prepare_tensors

# Per-epoch interception hook: ``fn(job_id, epoch)`` invoked after each epoch
# computes but before metrics/checkpoints are published. None in production;
# tests set it to flip pause/cancel control deterministically between epochs.
ON_EPOCH_HOOK = None


def set_epoch_hook(fn) -> None:
    global ON_EPOCH_HOOK
    ON_EPOCH_HOOK = fn



def _verify_dataset(ds) -> tuple[np.ndarray, np.ndarray]:
    """Re-validate whitelist containment and digest before touching data."""
    feat = data_safe.resolve_within_whitelist(ds.feature_path)
    tgt = None
    if ds.target_path:
        tgt = data_safe.resolve_within_whitelist(ds.target_path)
    summary = ds.summary_json
    if data_safe.file_digest(feat) != summary["feature_sha256"]:
        raise RuntimeError("dataset feature file changed since registration")
    if tgt is not None and data_safe.file_digest(tgt) != summary.get("target_sha256"):
        raise RuntimeError("dataset target file changed since registration")
    X, y = data_safe.load_dataset(str(feat), str(tgt) if tgt else None)
    return X, y


class LeaseWatchdog:
    """Background lease renewal. Sets ``lost`` when the lease is gone."""

    def __init__(self, db_factory, job_id: str, executor_id: str, interval: float):
        self._db_factory = db_factory
        self._job_id = job_id
        self._executor_id = executor_id
        self._interval = interval
        self._stop = threading.Event()
        self.lost = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            db: Session = self._db_factory()
            try:
                if not queue.heartbeat(db, self._job_id, self._executor_id):
                    self.lost = True
                    return
            finally:
                db.close()


def run_job(job_id: str, executor_id: str,
            db: Session | None = None) -> str:
    """Execute a claimed job to a terminal state or to a paused boundary.

    Returns the final status value.
    """
    own_session = db is None
    db = db or SessionLocal()
    from ..config import HEARTBEAT_INTERVAL
    watchdog = LeaseWatchdog(
        SessionLocal, job_id, executor_id, HEARTBEAT_INTERVAL
    )
    watchdog.start()
    try:
        return _run(db, job_id, executor_id, watchdog)
    except Exception as exc:  # noqa: BLE001 - record failure on the job
        db.rollback()
        job = db.get(Job, job_id)
        if job is not None and job.executor_id == executor_id and \
                job.status == JobStatus.RUNNING:
            job.status = JobStatus.FAILED
            job.error = str(exc)[:2000]
            job.finished_at = utcnow()
            db.commit()
            try:
                events.append_event(
                    db, job_id, "failed", {"error": str(exc)[:1000]})
            except Exception:
                pass
        return JobStatus.FAILED.value
    finally:
        watchdog.stop()
        if own_session:
            db.close()


def _run(db: Session, job_id: str, executor_id: str,
         watchdog: LeaseWatchdog) -> str:
    job = db.get(Job, job_id)
    if job is None:
        return JobStatus.FAILED.value

    from ..models.orm import Architecture, Dataset
    arch = db.get(Architecture, job.architecture_id)
    ds = db.get(Dataset, job.dataset_id)

    spec = parse_spec(arch.spec_json)
    hp = HyperParams.validate(job.hyperparams_json)
    X, y = _verify_dataset(ds)
    Xt, yt = prepare_tensors(spec, ds.task, X, y)

    # Stored indices are authoritative — a resume never re-splits.
    tr = np.asarray(job.train_idx_json, dtype=np.int64)
    va = np.asarray(job.val_idx_json, dtype=np.int64)
    Xtr, ytr = Xt[tr], yt[tr]
    Xval, yval = Xt[va], yt[va]

    engine = Engine(spec, hp, ds.task, job.seed)
    epochs_done = 0
    ckpt = latest_valid_checkpoint(db, job_id)
    if ckpt is not None:
        state = load_state(ckpt)
        epochs_done = engine.load_state_dict(state)
        # The job record's epochs_done mirrors the newest checkpoint.
        events.append_event(
            db, job_id, "resumed",
            {"from_epoch": epochs_done, "checkpoint_id": ckpt.id})
    else:
        events.append_event(db, job_id, "started",
                            {"epochs_total": job.epochs_total})

    if epochs_done >= job.epochs_total:
        _finish(db, job)
        return JobStatus.COMPLETED.value

    def boundary_decision(epoch: int) -> str | None:
        """Evaluate PAUSED/CANCELLED at an epoch boundary under a row lock.

        Returns the terminal status string ("paused"/"cancelled") when the job
        should stop, or None to keep training. Metrics/checkpoint for the
        just-finished epoch are already durable when this is called after an
        epoch; called before the first epoch it stops without publishing.
        """
        row = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if row.status in (JobStatus.CANCELLING, JobStatus.CANCELLED):
            row.status = JobStatus.CANCELLED
            row.executor_id = None
            row.lease_expires_at = None
            row.heartbeat_at = None
            row.finished_at = utcnow()
            db.commit()
            events.append_event(db, job_id, "cancelled", {"epoch": epoch})
            return JobStatus.CANCELLED.value
        if row.status == JobStatus.PAUSED:
            row.executor_id = None
            row.lease_expires_at = None
            row.heartbeat_at = None
            db.commit()
            events.append_event(db, job_id, "paused", {"epoch": epoch})
            return JobStatus.PAUSED.value
        return None

    for epoch in range(epochs_done + 1, job.epochs_total + 1):
        # Top-of-epoch boundary: a pause/cancel requested while the previous
        # epoch was finalizing is honoured before any new work begins.
        db.expire_all()
        decision = boundary_decision(epoch - 1)
        if decision is not None:
            return decision

        result = engine.run_epoch(Xtr, ytr, Xval, yval, epoch)

        # Re-read lease before publishing anything (stale executor must not
        # write metrics or checkpoints).
        db.expire_all()
        job = db.get(Job, job_id)
        if watchdog.lost or not queue.owns_active_lease(db, job_id, executor_id):
            # Lease lost: another executor owns the job now. Publish nothing.
            return job.status.value if job else JobStatus.QUEUED.value

        # Guarded metric publication — stale executor rejected above.
        payload = {
            "epoch": epoch,
            "train_loss": result.train_loss,
            "val_loss": result.val_loss,
        }
        if result.val_accuracy is not None:
            payload["val_accuracy"] = result.val_accuracy
        events.append_event(db, job_id, "epoch", payload)
        job.epochs_done = epoch

        # Durable checkpoint: atomic file first, reference published after.
        save_checkpoint(db, job, epoch, engine.state_dict(epoch))

        # Between-epochs interception point (no-op outside tests). The just
        # finished epoch's metrics and checkpoint are already durable; a test
        # flips pause/cancel here and the boundary decision immediately below
        # observes it deterministically (no racing sub-millisecond epochs).
        if ON_EPOCH_HOOK is not None:
            ON_EPOCH_HOOK(job_id, epoch)

        # End-of-epoch boundary decision (row lock makes pause/cancel races
        # deterministic against the control endpoints).
        decision = boundary_decision(epoch)
        if decision is not None:
            return decision
        # Still RUNNING: renew lease inline and continue.
        queue.heartbeat(db, job_id, executor_id)

    _finish(db, db.get(Job, job_id))
    events.append_event(db, job_id, "completed",
                        {"epochs": job.epochs_total})
    return JobStatus.COMPLETED.value


def _finish(db: Session, job: Job) -> None:
    job.status = JobStatus.COMPLETED
    job.executor_id = None
    job.lease_expires_at = None
    job.heartbeat_at = None
    job.finished_at = utcnow()
    db.commit()
