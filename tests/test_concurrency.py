"""Concurrent claim semantics: one winner, exact per-user cap."""
from __future__ import annotations

import threading
import uuid

import pytest

from app import queue as q
from app.config import get_settings
from app.db import SessionLocal
from app.models import Architecture, Dataset, Job, JobStatus
from app.datasets import inspect_dataset

from .conftest import make_arch_spec


def _seed_arch_dataset(db, csv_path):
    info = inspect_dataset(csv_path, settings=get_settings())
    arch = Architecture(name="a", spec=make_arch_spec(),
                        content_hash=uuid.uuid4().hex, param_count=10)
    ds = Dataset(name="d", path=info["resolved_path"], fmt=info["fmt"],
                 task="classification", num_rows=info["num_rows"],
                 num_features=info["num_features"], digest=info["digest"])
    db.add_all([arch, ds])
    db.flush()
    return arch.id, ds.id


def _enqueue(db, uid, arch_id, ds_id, n=1):
    ids = []
    for _ in range(n):
        job = q.create_job(db, user_id=uid, architecture_id=arch_id,
                           dataset_id=ds_id, total_epochs=1, seed=1,
                           hyperparams=None)
        ids.append(job.id)
    return ids


def test_two_executors_one_winner_per_job(csv_dataset):
    settings = get_settings()
    uid = f"c-{uuid.uuid4().hex[:8]}"
    with SessionLocal() as db:
        arch_id, ds_id = _seed_arch_dataset(db, csv_dataset)
        job_id = _enqueue(db, uid, arch_id, ds_id)[0]

    winners: list[int] = []
    barrier = threading.Barrier(2)

    def claim(exec_id: str):
        with SessionLocal() as db:
            barrier.wait()
            job = q.claim_job(db, executor_id=exec_id, settings=settings)
            if job is not None:
                winners.append(job.id)

    t1 = threading.Thread(target=claim, args=("exec-a",))
    t2 = threading.Thread(target=claim, args=("exec-b",))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert winners == [job_id]


def test_per_user_cap_exact_under_concurrency(csv_dataset):
    settings = get_settings()
    uid = f"cap-{uuid.uuid4().hex[:8]}"
    with SessionLocal() as db:
        arch_id, ds_id = _seed_arch_dataset(db, csv_dataset)
        ids = _enqueue(db, uid, arch_id, ds_id, n=5)

    results: dict[str, object] = {"claimed": [], "none": 0}
    lock = threading.Lock()

    def claim(i: int):
        with SessionLocal() as db:
            job = q.claim_job(db, executor_id=f"exec-{i}", settings=settings)
            with lock:
                if job is None:
                    results["none"] += 1
                else:
                    results["claimed"].append(job.id)

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(5)]
    # Stagger slightly so claims genuinely contend under the global lock.
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly cap (3) jobs claimed; the remaining queued jobs of the same
    # user cannot be claimed once the cap is reached.
    assert sorted(results["claimed"]) == sorted(ids[: settings.max_running_per_user])
    assert results["none"] == 5 - settings.max_running_per_user
    with SessionLocal() as db:
        running = [
            j.id for j in db.scalars(
                q.select(Job).where(Job.user_id == uid)
            ).all() if j.status == JobStatus.RUNNING
        ]
        assert sorted(running) == sorted(results["claimed"])


def test_other_user_jobs_not_blocked_by_cap(csv_dataset):
    settings = get_settings()
    u1 = f"u1-{uuid.uuid4().hex[:6]}"
    u2 = f"u2-{uuid.uuid4().hex[:6]}"
    with SessionLocal() as db:
        arch_id, ds_id = _seed_arch_dataset(db, csv_dataset)
        u1_ids = _enqueue(db, u1, arch_id, ds_id, n=5)
        j2 = _enqueue(db, u2, arch_id, ds_id, n=1)[0]

    claimed = []
    with SessionLocal() as db:
        for i in range(3):
            j = q.claim_job(db, executor_id=f"e{i}", settings=settings)
            assert j is not None
            claimed.append(j.id)
        assert set(claimed) <= set(u1_ids)
        # Next claim walks past the remaining (capped) u1 candidates and
        # claims the other user's job.
        j = q.claim_job(db, executor_id="other", settings=settings)
        assert j is not None and j.id == j2
