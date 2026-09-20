"""Lease expiry/reclaim and runner-level checkpoint corruption fallback."""
from __future__ import annotations

import os
import uuid
from datetime import timedelta

from app import queue as q
from app import checkpoints as ck
from app.config import get_settings
from app.db import SessionLocal
from app.datasets import inspect_dataset
from app.models import Architecture, Dataset, Job, JobStatus
from app.runner import run_job, TEST_HOOKS

from .conftest import make_arch_spec
from .helpers import list_job_events


def _setup(csv_path, uid=None, epochs=4, hp=None, seed=5):
    settings = get_settings()
    info = inspect_dataset(csv_path, settings=settings)
    uid = uid or f"lf-{uuid.uuid4().hex[:8]}"
    with SessionLocal() as db:
        arch = Architecture(name="a", spec=make_arch_spec(dropout=0.0),
                            content_hash=uuid.uuid4().hex, param_count=10)
        ds = Dataset(name="d", path=info["resolved_path"], fmt=info["fmt"],
                     task="classification", num_rows=info["num_rows"],
                     num_features=info["num_features"], digest=info["digest"])
        db.add_all([arch, ds]); db.flush()
        job = q.create_job(db, user_id=uid, architecture_id=arch.id,
                           dataset_id=ds.id, total_epochs=epochs, seed=seed,
                           hyperparams=hp or {"batch_size": 16, "lr": 0.05,
                                              "val_fraction": 0.25},
                           settings=settings)
        return job.id, uid


def test_expired_lease_is_requeued_then_resumed(csv_dataset):
    job_id, uid = _setup(csv_dataset, epochs=4)
    settings = get_settings()
    old = "old-exec"
    with SessionLocal() as db:
        assert q.claim_job(db, executor_id=old, settings=settings).id == job_id
        # Expire the lease manually and reap.
        job = db.get(Job, job_id)
        job.lease_expires_at = job.lease_expires_at - timedelta(hours=1)
        db.commit()
    with SessionLocal() as db:
        assert q.reap_expired_leases(db) == 1
        job = db.get(Job, job_id)
        assert job.status == JobStatus.QUEUED
        assert job.executor_id is None
    # A new executor claims and completes the full run from scratch.
    with SessionLocal() as db:
        assert q.claim_job(db, executor_id="new-exec", settings=settings).id == job_id
    with SessionLocal() as db:
        assert run_job(db, db.get(Job, job_id), settings=settings) == "completed"
    with SessionLocal() as db:
        assert db.get(Job, job_id).epochs_completed == 4


def test_runner_falls_back_to_previous_good_checkpoint(csv_dataset):
    """Pause after epoch 3, corrupt epoch-3 file, resume -> epoch 3 metrics
    are re-emitted and the run completes using the epoch-2 checkpoint."""
    job_id, uid = _setup(csv_dataset, epochs=5, seed=77)
    settings = get_settings()

    def hook(jid, epoch, status):
        if epoch == 3 and status == "running":
            with SessionLocal() as hdb:
                q.pause_job(hdb, jid, uid)
            return True

    TEST_HOOKS[job_id] = hook
    with SessionLocal() as db:
        assert q.claim_job(db, executor_id="first", settings=settings) is not None
        assert run_job(db, db.get(Job, job_id), settings=settings) == "paused"

    # Corrupt the newest checkpoint file on disk (simulates partial write /
    # disk damage discovered on resume).
    with SessionLocal() as db:
        refs = db.get(Job, job_id).checkpoints
        newest = max(refs, key=lambda r: r.epoch)
        assert newest.epoch == 3
        with open(newest.path, "wb") as f:
            f.write(b"<<corrupt>>")

    events_before = list_job_events(job_id)
    with SessionLocal() as db:
        q.resume_job(db, job_id, uid)
    with SessionLocal() as db:
        assert q.claim_job(db, executor_id="second", settings=settings) is not None
    with SessionLocal() as db:
        status = run_job(db, db.get(Job, job_id), settings=settings)
    assert status == "completed"

    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.epochs_completed == 5
        # Epoch 3 was re-trained after the fallback; its reference was
        # republished (re-validated) pointing at the fresh atomic file.
        surviving = {r.epoch: r for r in job.checkpoints}
        assert set(surviving) <= {3, 4, 5}  # older refs pruned (keep 3)
        assert surviving[3].valid is True
        assert ck.is_valid_file(surviving[3].path, expected_epoch=3)
    events_after = list_job_events(job_id)
    assert any(
        e["kind"] == "log" and "resumed from epoch 2" in e["payload"]["message"]
        for e in events_after
    )
    # Epoch 3 was re-trained after fallback, so a second epoch-3 metrics
    # event exists after the resume event.
    metrics3 = [
        e for e in events_after
        if e["kind"] == "metrics" and e["payload"]["epoch"] == 3
    ]
    assert len(metrics3) == 2
    assert len(events_after) > len(events_before)
