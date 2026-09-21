"""End-to-end runner: corruption rollback, resume consistency, cancellation."""
from __future__ import annotations

import threading

import numpy as np
import pytest

from app.db import SessionLocal
from app.models.orm import Checkpoint, Job, JobStatus
from app.services import data_safe, jobs
from app.workers.runner import run_job
from app.services import events as events_svc
from app.services.checkpoints import latest_valid_checkpoint
from app.services.training import (
    Engine,
    HyperParams,
    prepare_tensors,
)


def _register(db, sample_data, arch_spec, name="m"):
    arch = jobs.create_architecture(db, name, arch_spec)
    ds = jobs.register_dataset(db, str(sample_data["csv"]), None,
                               "classification")
    return arch, ds


def _claim_and_run_once(job_id, expected_id=None):
    """Claim and run a job on a *fresh* session (the runner runs in a
    separate thread and SQLAlchemy sessions must not be shared)."""
    from app.services import queue
    executor = f"exec-{job_id[:6]}-{threading.get_ident()}"
    s = SessionLocal()
    try:
        claimed = queue.claim_next_job(s, executor)
        assert claimed is not None
        if expected_id is not None:
            assert claimed.id == expected_id
    finally:
        s.close()
    return run_job(job_id, executor)


def _run_with_control_at_epoch(job_id, user, action, at_epoch):
    """Run a job in a background thread; flip control state deterministically
    the moment the runner finishes epoch ``at_epoch`` (via the engine hook),
    so the boundary decision on the *next* epoch is guaranteed to observe it.
    Returns (thread, final_status_holder).
    """
    from app.workers import runner as runner_mod

    holder = {}

    def hook(jid, epoch):
        if epoch == at_epoch:
            s = SessionLocal()
            try:
                if action == "pause":
                    jobs.request_pause(s, jid, user)
                elif action == "cancel":
                    jobs.request_cancel(s, jid, user)
                elif action == "pause_then_cancel":
                    jobs.request_pause(s, jid, user)
                    jobs.request_cancel(s, jid, user)
                elif action == "cancel_then_pause":
                    jobs.request_cancel(s, jid, user)
                    try:
                        jobs.request_pause(s, jid, user)
                    except jobs.Conflict:
                        pass
            finally:
                s.close()

    def target():
        runner_mod.set_epoch_hook(hook)
        try:
            holder["final"] = _claim_and_run_once(job_id)
        finally:
            runner_mod.set_epoch_hook(None)

    t = threading.Thread(target=target)
    t.start()
    return t, holder


def test_full_run_emits_real_metrics_and_completes(
        db, sample_data, arch_spec):
    arch, ds = _register(db, sample_data, arch_spec)
    job = jobs.create_job(
        db, "alice", arch.id, ds.id,
        {"lr": 0.05, "batch_size": 16}, epochs=4, seed=3, val_fraction=0.25)
    final = _claim_and_run_once(job.id)
    assert final == JobStatus.COMPLETED.value
    db.refresh(job)
    assert job.epochs_done == 4
    rows = events_svc.read_events(db, job.id)
    kinds = [r.kind for r in rows]
    assert kinds[0] == "started"
    assert kinds[-1] == "completed"
    epoch_events = [r for r in rows if r.kind == "epoch"]
    assert len(epoch_events) == 4
    for r in epoch_events:
        assert "train_loss" in r.payload_json and "val_loss" in r.payload_json
    # gapless sequence numbers
    seqs = [r.seq for r in rows]
    assert seqs == list(range(1, len(seqs) + 1))


def test_corrupt_checkpoint_rolls_back_to_latest_valid(
        db, sample_data, arch_spec, monkeypatch):
    from app.config import MAX_CHECKPOINTS
    # Keep all 4 checkpoints for this test.
    monkeypatch.setattr("app.services.checkpoints.MAX_CHECKPOINTS", 10)

    arch, ds = _register(db, sample_data, arch_spec)
    job = jobs.create_job(
        db, "alice", arch.id, ds.id,
        {"lr": 0.05, "batch_size": 16}, epochs=4, seed=5, val_fraction=0.25)

    # Run 2 epochs, then pause deterministically at the epoch-2 boundary.
    t, holder = _run_with_control_at_epoch(job.id, "alice", "pause", 2)
    t.join(timeout=15)
    db.refresh(job)
    assert holder["final"] == JobStatus.PAUSED.value
    assert job.status == JobStatus.PAUSED
    assert job.epochs_done == 2

    # Corrupt the newest checkpoint (epoch 2).
    ckpts = db.query(Checkpoint).filter_by(job_id=job.id).order_by(
        Checkpoint.epoch.asc()).all()
    newest = ckpts[-1]
    with open(newest.path, "wb") as fh:
        fh.write(b"not a checkpoint file at all")

    # latest valid must now be epoch 1 and the corrupt row flagged.
    fallback = latest_valid_checkpoint(db, job.id)
    assert fallback.epoch == 1
    db.refresh(newest)
    assert newest.valid is False

    # Resume: the runner must recover from epoch 1 without error and finish.
    jobs.request_resume(db, job.id, "alice")
    final = _claim_and_run_once(job.id)
    assert final == JobStatus.COMPLETED.value
    db.refresh(job)
    assert job.epochs_done == 4


def test_resume_matches_continuous_within_tolerance(
        db, sample_data, arch_spec, monkeypatch):
    monkeypatch.setattr("app.services.checkpoints.MAX_CHECKPOINTS", 10)
    arch, ds = _register(db, sample_data, arch_spec)
    seed, epochs = 11, 6
    hp = {"lr": 0.07, "batch_size": 12}

    # --- continuous reference, computed independently with the same contract
    X, y = data_safe.load_dataset(ds.feature_path, ds.target_path)
    from app.models.network_spec import parse_spec
    spec = parse_spec(arch.spec_json)
    Xt, yt = prepare_tensors(spec, ds.task, X, y)

    job_probe = jobs.create_job(
        db, "ref", arch.id, ds.id, hp, epochs=epochs, seed=seed,
        val_fraction=0.25)
    tr = np.asarray(job_probe.train_idx_json, dtype=np.int64)
    va = np.asarray(job_probe.val_idx_json, dtype=np.int64)
    Xtr, ytr, Xval, yval = Xt[tr], yt[tr], Xt[va], yt[va]
    db.delete(job_probe)
    db.commit()

    # Continuous reference, constructed exactly the way the runner builds its
    # Engine (the Engine ctor itself seeds all RNG; no extra seeding that
    # would advance the stream differently).
    eng = Engine(spec, HyperParams.validate(hp), "classification", seed)
    continuous = [eng.run_epoch(Xtr, ytr, Xval, yval, i).train_loss
                  for i in range(1, epochs + 1)]

    # --- interrupted job: pause after epoch 3, then resume
    job = jobs.create_job(
        db, "alice", arch.id, ds.id, hp, epochs=epochs, seed=seed,
        val_fraction=0.25)
    # Identical split contract (same seed & digest).
    assert job.train_idx_json == list(tr)

    t, holder = _run_with_control_at_epoch(job.id, "alice", "pause", 3)
    t.join(timeout=15)
    db.refresh(job)
    assert holder["final"] == JobStatus.PAUSED.value
    assert job.epochs_done == 3

    jobs.request_resume(db, job.id, "alice")
    final = _claim_and_run_once(job.id)
    assert final == JobStatus.COMPLETED.value

    rows = events_svc.read_events(db, job.id)
    got = [r.payload_json["train_loss"]
           for r in rows if r.kind == "epoch"]
    assert len(got) == epochs
    for i, (a, b) in enumerate(zip(continuous, got), start=1):
        assert np.isclose(a, b, rtol=1e-4, atol=1e-5), \
            f"epoch {i}: continuous {a} vs resumed {b}"


def test_resume_does_not_resplit(db, sample_data, arch_spec, monkeypatch):
    monkeypatch.setattr("app.services.checkpoints.MAX_CHECKPOINTS", 10)
    arch, ds = _register(db, sample_data, arch_spec)
    job = jobs.create_job(
        db, "alice", arch.id, ds.id,
        {"lr": 0.05, "batch_size": 16}, epochs=3, seed=8, val_fraction=0.3)
    before = (list(job.train_idx_json), list(job.val_idx_json))

    t, holder = _run_with_control_at_epoch(job.id, "alice", "pause", 2)
    t.join(timeout=15)
    db.expire_all()
    assert holder["final"] == JobStatus.PAUSED.value
    jobs.request_resume(db, job.id, "alice")
    _claim_and_run_once(job.id)
    db.expire_all()
    assert list(db.get(Job, job.id).train_idx_json) == before[0]
    assert list(db.get(Job, job.id).val_idx_json) == before[1]


def test_cancel_race_at_epoch_boundary(db, sample_data, arch_spec):
    """Pause and cancel requested in immediate succession at an epoch
    boundary; the row-locked control transition must end the job
    deterministically — never crashed/double-terminal, never resurrected."""
    arch, ds = _register(db, sample_data, arch_spec)
    outcomes = []
    for ordering, action in (("pause-first", "pause_then_cancel"),
                             ("cancel-first", "cancel_then_pause")):
        job = jobs.create_job(
            db, f"u-{ordering}", arch.id, ds.id,
            {"lr": 0.05, "batch_size": 16}, epochs=8, seed=4,
            val_fraction=0.25)
        t, holder = _run_with_control_at_epoch(
            job.id, f"u-{ordering}", action, 3)
        t.join(timeout=15)
        db.refresh(job)
        # pause-then-cancel: cancel wins (latest committed decision).
        # cancel-then-pause: pause is rejected (409 conflict), cancel wins.
        assert holder["final"] == JobStatus.CANCELLED.value
        assert job.status == JobStatus.CANCELLED
        outcomes.append(job.status)
    assert outcomes == [JobStatus.CANCELLED, JobStatus.CANCELLED]


def test_pause_at_boundary_is_resumable(db, sample_data, arch_spec):
    """A pure pause at the boundary leaves a PAUSED job with a valid
    checkpoint; resuming completes it (the benign counterpart of the cancel
    race)."""
    arch, ds = _register(db, sample_data, arch_spec)
    job = jobs.create_job(
        db, "alice", arch.id, ds.id,
        {"lr": 0.05, "batch_size": 16}, epochs=5, seed=4, val_fraction=0.25)
    t, holder = _run_with_control_at_epoch(job.id, "alice", "pause", 2)
    t.join(timeout=15)
    db.refresh(job)
    assert holder["final"] == JobStatus.PAUSED.value
    assert job.status == JobStatus.PAUSED
    assert job.epochs_done == 2
    jobs.request_resume(db, job.id, "alice")
    assert _claim_and_run_once(job.id) == JobStatus.COMPLETED.value
    db.refresh(job)
    assert job.epochs_done == 5


def test_cancel_queued_job_is_immediate(db, sample_data, arch_spec):
    arch, ds = _register(db, sample_data, arch_spec)
    job = jobs.create_job(
        db, "alice", arch.id, ds.id,
        {"lr": 0.05, "batch_size": 16}, epochs=2, seed=1, val_fraction=0.25)
    jobs.request_cancel(db, job.id, "alice")
    db.refresh(job)
    assert job.status == JobStatus.CANCELLED


def test_dataset_shape_mismatch_fails_job(db, sample_data):
    # Architecture expects 4 features but data has 2 -> 422 at creation.
    spec = {
        "layers": [
            {"id": "in", "type": "input", "in_features": 4},
            {"id": "o", "type": "dense", "out_features": 3},
        ],
        "connections": [["in", "o"]],
    }
    arch = jobs.create_architecture(db, "m", spec)
    ds = jobs.register_dataset(db, str(sample_data["csv"]), None,
                               "classification")
    with pytest.raises(ValueError, match="feature width"):
        jobs.create_job(
            db, "alice", arch.id, ds.id,
            {"lr": 0.05, "batch_size": 16}, epochs=2, seed=1,
            val_fraction=0.25)


def test_stale_executor_cannot_submit_after_losing_lease(
        db, sample_data, arch_spec):
    """Owns-active-lease guard rejects metrics/checkpoints from a superseded
    executor."""
    arch, ds = _register(db, sample_data, arch_spec)
    job = jobs.create_job(
        db, "alice", arch.id, ds.id,
        {"lr": 0.05, "batch_size": 16}, epochs=3, seed=2, val_fraction=0.25)
    from app.services import queue
    s = SessionLocal()
    claimed = queue.claim_next_job(s, "exec-A")
    assert claimed is not None
    # Simulate lease expiry + requeue + another executor taking over.
    from datetime import datetime, timedelta, timezone
    j = s.get(Job, job.id)
    j.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    s.commit()
    queue.requeue_expired_leases(s)
    claimed2 = queue.claim_next_job(s, "exec-B")
    assert claimed2 is not None and claimed2.id == job.id
    # A is now stale and must not be able to write anything.
    assert queue.owns_active_lease(s, job.id, "exec-A") is False
    assert queue.owns_active_lease(s, job.id, "exec-B") is True
    s.close()
