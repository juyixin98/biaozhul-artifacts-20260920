"""Queue semantics: concurrent claims and the per-user running cap."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from app.db import SessionLocal
from app.models.orm import Job, JobStatus
from app.services import jobs, queue
from app.services.jobs import Conflict


def _make_arch_and_data(db, sample_data, arch_spec):
    arch = jobs.create_architecture(db, "m", arch_spec)
    ds = jobs.register_dataset(db, str(sample_data["csv"]), None, "classification")
    return arch, ds


def test_concurrent_claims_never_double_assign(db, sample_data, arch_spec):
    arch, ds = _make_arch_and_data(db, sample_data, arch_spec)
    created = []
    for i in range(10):
        job = jobs.create_job(
            db, f"user{i % 4}", arch.id, ds.id,
            {"lr": 0.05, "batch_size": 8}, epochs=3, seed=i,
            val_fraction=0.2)
        created.append(job.id)

    claimed: list[str] = []

    def attempt(n):
        s = SessionLocal()
        try:
            j = queue.claim_next_job(s, f"exec-{n}")
            return j.id if j else None
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(attempt, range(10)))

    winners = [r for r in results if r]
    assert len(winners) == len(set(winners)), "a job was claimed twice"
    assert len(winners) == 10


def test_per_user_three_job_cap(db, sample_data, arch_spec):
    arch, ds = _make_arch_and_data(db, sample_data, arch_spec)
    for i in range(5):
        jobs.create_job(
            db, "alice", arch.id, ds.id,
            {"lr": 0.05, "batch_size": 8}, epochs=2, seed=i,
            val_fraction=0.2)
    # bob's job should be claimable even while alice saturates her quota.
    bob = jobs.create_job(
        db, "bob", arch.id, ds.id,
        {"lr": 0.05, "batch_size": 8}, epochs=2, seed=99,
        val_fraction=0.2)

    claimed_alice = 0
    for n in range(5):
        s = SessionLocal()
        j = queue.claim_next_job(s, f"exec-{n}")
        if j:
            claimed_alice += 1 if j.user_id == "alice" else 0
            if j.user_id == "bob":
                bob_claimed = j.id
        s.close()
    assert claimed_alice == 3
    # alice's 4th/5th jobs still queued
    queued = db.query(Job).filter_by(user_id="alice",
                                     status=JobStatus.QUEUED).count()
    assert queued == 2


def test_stale_executor_heartbeat_rejected(db, sample_data, arch_spec):
    arch, ds = _make_arch_and_data(db, sample_data, arch_spec)
    job = jobs.create_job(
        db, "alice", arch.id, ds.id,
        {"lr": 0.05, "batch_size": 8}, epochs=2, seed=1, val_fraction=0.2)
    s = SessionLocal()
    j = queue.claim_next_job(s, "exec-old")
    assert j is not None
    # new executor identity can't renew the old executor's lease
    assert queue.heartbeat(s, j.id, "exec-impostor") is False
    assert queue.owns_active_lease(s, j.id, "exec-impostor") is False
    s.close()


def test_expired_lease_is_requeued(db, sample_data, arch_spec, monkeypatch):
    from app.services import queue as q
    arch, ds = _make_arch_and_data(db, sample_data, arch_spec)
    job = jobs.create_job(
        db, "alice", arch.id, ds.id,
        {"lr": 0.05, "batch_size": 8}, epochs=2, seed=1, val_fraction=0.2)
    s = SessionLocal()
    assert queue.claim_next_job(s, "exec-old") is not None
    # Force the lease into the past.
    from datetime import datetime, timedelta, timezone
    j = s.get(Job, job.id)
    j.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    s.commit()
    assert queue.requeue_expired_leases(s) == 1
    j = s.get(Job, job.id)
    assert j.status == JobStatus.QUEUED
    assert j.executor_id is None
    # old executor is now rejected
    assert queue.owns_active_lease(s, job.id, "exec-old") is False
    s.close()
