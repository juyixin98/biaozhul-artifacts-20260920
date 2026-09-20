"""Deterministic resume: paused-and-resumed run == uninterrupted run.

We compare per-epoch metrics (train/val loss) between:
  A) one continuous run of N epochs;
  B) a run paused after every epoch and resumed by fresh executors.

All runs share seed, split snapshot and dataset; expected difference is 0
bitwise thanks to per-epoch seeded generators, with a declared tolerance of
1e-6 (see README).
"""
from __future__ import annotations

import uuid

from app import queue as q
from app.config import get_settings
from app.db import SessionLocal
from app.datasets import inspect_dataset
from app.models import Architecture, Dataset, Job, JobStatus

from .conftest import make_arch_spec
from .helpers import list_job_events, run_to_end, wait_for_status


def _setup(csv_path, seed=2026, epochs=6, hp=None):
    settings = get_settings()
    info = inspect_dataset(csv_path, settings=settings)
    arch_spec = make_arch_spec(hidden=12, out=3, dropout=0.25)
    uid = f"det-{uuid.uuid4().hex[:8]}"
    with SessionLocal() as db:
        arch = Architecture(name="a", spec=arch_spec,
                            content_hash=uuid.uuid4().hex, param_count=10)
        ds = Dataset(name="d", path=info["resolved_path"], fmt=info["fmt"],
                     task="classification", num_rows=info["num_rows"],
                     num_features=info["num_features"], digest=info["digest"])
        db.add_all([arch, ds]); db.flush()
        job = q.create_job(
            db, user_id=uid, architecture_id=arch.id, dataset_id=ds.id,
            total_epochs=epochs, seed=seed, hyperparams=hp, settings=settings,
        )
        return job.id, job.split


def _metric_series(job_id):
    events = list_job_events(job_id)
    return [e["payload"] for e in events if e["kind"] == "metrics"]


def _pause_after_next_epoch(job_id):
    """Pause deterministically at the next epoch boundary via runner hook."""
    from app.runner import TEST_HOOKS

    def hook(jid, epoch, status):
        if status == "running":
            with SessionLocal() as hdb:
                q.pause_job(hdb, jid, hdb.get(Job, jid).user_id)
            return True

    TEST_HOOKS[job_id] = hook
    status = run_to_end(job_id)
    assert status == "paused"
    with SessionLocal() as db:
        return db.get(Job, job_id).epochs_completed


def test_resumed_run_matches_continuous_run(csv_dataset):
    hp = {"batch_size": 16, "lr": 0.05, "val_fraction": 0.25}
    epochs = 6
    id_a, split_a = _setup(csv_dataset, seed=2026, epochs=epochs, hp=hp)
    id_b, split_b = _setup(csv_dataset, seed=2026, epochs=epochs, hp=hp)
    assert split_a == split_b

    # A: continuous
    assert run_to_end(id_a) == "completed"
    series_a = _metric_series(id_a)

    # B: pause after epoch 1, resume; pause again; resume to end.
    done1 = _pause_after_next_epoch(id_b)
    assert done1 == 1
    with SessionLocal() as db:
        q.resume_job(db, id_b, db.get(Job, id_b).user_id)
    done2 = _pause_after_next_epoch(id_b)
    assert done2 == 2
    with SessionLocal() as db:
        q.resume_job(db, id_b, db.get(Job, id_b).user_id)
    assert run_to_end(id_b) == "completed"

    series_b = _metric_series(id_b)
    assert len(series_a) == len(series_b) == epochs
    for pa, pb in zip(series_a, series_b):
        assert pa["epoch"] == pb["epoch"]
        for key in ("train_loss", "val_loss", "val_accuracy"):
            assert abs(pa[key] - pb[key]) < 1e-6, (key, pa, pb)


def test_resume_uses_fixed_split_not_resplit(csv_dataset):
    hp = {"batch_size": 16, "lr": 0.05, "val_fraction": 0.25}
    job_id, split = _setup(csv_dataset, seed=99, epochs=3, hp=hp)
    done = _pause_after_next_epoch(job_id)
    assert done >= 1
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.split == split  # unchanged across resume
        q.resume_job(db, job_id, job.user_id)
    run_to_end(job_id)
    with SessionLocal() as db:
        assert db.get(Job, job_id).split == split


def test_resume_is_bitwise_at_model_state_level(csv_dataset):
    """Load both jobs' final checkpoints and compare state dicts."""
    import torch
    from app import checkpoints as ck

    hp = {"batch_size": 16, "lr": 0.05, "val_fraction": 0.25}
    id_a, _ = _setup(csv_dataset, seed=4242, epochs=3, hp=hp)
    id_b, _ = _setup(csv_dataset, seed=4242, epochs=3, hp=hp)
    run_to_end(id_a)
    done = _pause_after_next_epoch(id_b)
    assert done == 1
    with SessionLocal() as db:
        q.resume_job(db, id_b, db.get(Job, id_b).user_id)
    run_to_end(id_b)

    def last_state(jid):
        with SessionLocal() as db:
            refs = db.get(Job, jid).checkpoints
            ref = sorted(refs, key=lambda r: r.epoch)[-1]
            payload = torch.load(ref.path, map_location="cpu", weights_only=False)
            return payload["model_state"]

    sa, sb = last_state(id_a), last_state(id_b)
    assert set(sa) == set(sb)
    for k in sa:
        assert torch.equal(sa[k], sb[k]), f"divergent weights at {k}"
