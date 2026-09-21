"""Shared pytest fixtures.

Tests run against a real PostgreSQL instance (``nn_training_test`` by default)
because the queue relies on ``SELECT ... FOR UPDATE SKIP LOCKED``. Each test
gets a clean schema.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://nn:nn@localhost:5432/nn_training_test",
)
TMP_WHITELIST = Path("/tmp/nnworkflow_test_data")
TMP_WHITELIST.mkdir(parents=True, exist_ok=True)
os.environ["DATA_WHITELIST"] = str(TMP_WHITELIST)
os.environ["CHECKPOINT_DIR"] = "/tmp/nnworkflow_test_ckpt"
os.environ["LEASE_SECONDS"] = "30"

from app.db import Base, SessionLocal, engine, init_db  # noqa: E402
from app.models import orm  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def whitelist(tmp_path, monkeypatch):
    """Use pytest's per-test directory as the sole whitelist root."""
    root = tmp_path / "data"
    root.mkdir()
    from app import config
    resolved = root.resolve()
    monkeypatch.setattr(config, "data_whitelist", lambda: [resolved])
    return resolved


@pytest.fixture()
def sample_data(whitelist):
    rng = np.random.default_rng(7)
    centers = np.array([[2.0, 1.0], [-1.5, -1.0], [0.5, 2.0]], dtype=np.float32)
    labels = rng.integers(0, 3, size=90)
    X = centers[labels] + rng.normal(scale=0.5, size=(90, 2)).astype(np.float32)
    csv_path = whitelist / "samples.csv"
    with open(csv_path, "w") as fh:
        for row, lab in zip(X, labels):
            fh.write(f"{row[0]:.5f},{row[1]:.5f},{int(lab)}\n")
    np.save(whitelist / "X.npy", X)
    np.save(whitelist / "y.npy", labels.astype(np.int64))
    return {"root": whitelist, "csv": csv_path,
            "npy_x": whitelist / "X.npy", "npy_y": whitelist / "y.npy",
            "X": X, "y": labels}


@pytest.fixture()
def arch_spec():
    return {
        "layers": [
            {"id": "in", "type": "input", "in_features": 2},
            {"id": "h", "type": "dense", "out_features": 8},
            {"id": "a", "type": "relu"},
            {"id": "d", "type": "dropout", "p": 0.2},
            {"id": "out", "type": "dense", "out_features": 3},
        ],
        "connections": [["in", "h"], ["h", "a"], ["a", "d"], ["d", "out"]],
    }
