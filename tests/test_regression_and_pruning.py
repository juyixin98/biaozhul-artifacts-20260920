"""Regression task and checkpoint retention behaviour."""
from __future__ import annotations

import numpy as np

from app.models.orm import Checkpoint, Job, JobStatus
from app.services import events as events_svc
from app.services import jobs


def _register_regression(db, whitelist):
    rng = np.random.default_rng(3)
    X = rng.normal(size=(80, 3)).astype(np.float32)
    w = np.array([1.5, -2.0, 0.5], dtype=np.float32)
    y = (X @ w + 0.1 * rng.normal(size=80)).astype(np.float32)
    path = whitelist / "reg.csv"
    with open(path, "w") as fh:
        for row, target in zip(X, y):
            fh.write(f"{row[0]:.5f},{row[1]:.5f},{row[2]:.5f},{target:.5f}\n")
    arch = jobs.create_architecture(db, "regnet", {
        "layers": [
            {"id": "in", "type": "input", "in_features": 3},
            {"id": "h", "type": "dense", "out_features": 8},
            {"id": "a", "type": "relu"},
            {"id": "out", "type": "dense", "out_features": 1},
        ],
        "connections": [["in", "h"], ["h", "a"], ["a", "out"]],
    })
    ds = jobs.register_dataset(db, str(path), None, "regression")
    return arch, ds


def test_regression_job_trains_and_completes(db, whitelist):
    from tests.test_runner_integration import _claim_and_run_once
    arch, ds = _register_regression(db, whitelist)
    job = jobs.create_job(
        db, "reg", arch.id, ds.id,
        {"lr": 0.02, "batch_size": 16}, epochs=4, seed=2, val_fraction=0.25)
    final = _claim_and_run_once(job.id)
    assert final == JobStatus.COMPLETED.value
    rows = events_svc.read_events(db, job.id)
    epoch_events = [r for r in rows if r.kind == "epoch"]
    assert len(epoch_events) == 4
    # Regression events carry losses but never an accuracy.
    for r in epoch_events:
        assert "train_loss" in r.payload_json
        assert "val_accuracy" not in r.payload_json
    # A trained regressor should improve loss meaningfully over the run.
    assert epoch_events[-1].payload_json["val_loss"] < \
        epoch_events[0].payload_json["val_loss"]


def test_regression_requires_single_output():
    # Regression requires exactly 1 output unit (enforced when the loss is
    # built for the engine).
    import pytest
    from app.services.training import build_loss
    with pytest.raises(ValueError, match="1 unit"):
        build_loss("regression", 2)
    build_loss("regression", 1)  # single output is fine
    build_loss("classification", 3)


def test_checkpoint_pruning_keeps_newest_n(db, sample_data, arch_spec,
                                          monkeypatch):
    from tests.test_runner_integration import _claim_and_run_once
    monkeypatch.setattr("app.services.checkpoints.MAX_CHECKPOINTS", 2)
    arch = jobs.create_architecture(db, "p", arch_spec)
    ds = jobs.register_dataset(db, str(sample_data["csv"]), None,
                               "classification")
    job = jobs.create_job(
        db, "alice", arch.id, ds.id,
        {"lr": 0.05, "batch_size": 16}, epochs=5, seed=1, val_fraction=0.25)
    assert _claim_and_run_once(job.id) == JobStatus.COMPLETED.value
    valid = db.query(Checkpoint).filter_by(
        job_id=job.id, valid=True).all()
    assert len(valid) == 2
    epochs = sorted(c.epoch for c in valid)
    assert epochs == [4, 5]  # newest two retained
    # Pruned files were removed from disk.
    from pathlib import Path
    kept_epoch1 = Path(valid[0].path).parent / "ckpt-00001.pt"
    assert not kept_epoch1.exists()
