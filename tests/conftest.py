"""Pytest setup.

Environment variables are set *before* any app module is imported, because
``app.config.get_settings`` is cached at first use and the engine is built at
import time of ``app.db``.
"""
from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="nnlab-test-"))
_WHITELIST = _TMP / "data"
_WHITELIST.mkdir(parents=True, exist_ok=True)
_CKPT = _TMP / "ckpt"
_CKPT.mkdir(parents=True, exist_ok=True)

os.environ["DATABASE_URL"] = (
    "postgresql+psycopg2://trainer:trainer@localhost:5432/nn_training_test"
)
os.environ["DATA_WHITELIST_DIRS"] = str(_WHITELIST)
os.environ["CHECKPOINT_DIR"] = str(_CKPT)
os.environ["WORKER_ENABLED"] = "0"  # tests drive the runner synchronously
os.environ["LEADER_LEASE_SECONDS"] = "30"

import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402

WHITELIST = _WHITELIST
CKPT_DIR = _CKPT


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture()
def db():
    # Clean row content between tests (drop + recreate for true isolation).
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as session:
        yield session


@pytest.fixture()
def client(db):
    # init_db is a no-op when tables exist; worker disabled via env.
    with TestClient(app) as c:
        yield c


def unique_user() -> str:
    return f"u-{uuid.uuid4().hex[:10]}"


def auth(uid: str) -> dict[str, str]:
    return {"X-User-Id": uid}


@pytest.fixture()
def csv_dataset(tmp_under_whitelist):
    """120-row, 4-feature, 3-class csv dataset path inside the whitelist."""
    import csv

    rng = np.random.default_rng(42)
    centers = np.array([[0.0, 0.0, 0.0, 0.0],
                        [3.0, 3.0, 3.0, 3.0],
                        [-3.0, 3.0, -3.0, 3.0]])
    xs, ys = [], []
    for cls, c in enumerate(centers):
        xs.append(c[None, :] + rng.normal(scale=0.5, size=(40, 4)))
        ys.append(np.full(40, cls))
    x = np.vstack(xs).astype(np.float32)
    y = np.concatenate(ys)
    perm = rng.permutation(len(y))
    x, y = x[perm], y[perm]
    path = tmp_under_whitelist("cls.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["f1", "f2", "f3", "f4", "label"])
        for row, label in zip(x, y):
            w.writerow([f"{v:.5f}" for v in row] + [int(label)])
    return path


@pytest.fixture()
def npy_dataset(tmp_under_whitelist):
    rng = np.random.default_rng(0)
    x1 = rng.uniform(-1, 1, 80)
    x2 = rng.normal(size=80)
    y = (x1 * 2 + x2).astype(np.float32)
    arr = np.stack([x1, x2, y], axis=1).astype(np.float32)
    path = tmp_under_whitelist("reg.npy")
    np.save(path, arr)
    return path


@pytest.fixture()
def tmp_under_whitelist():
    def _make(name: str) -> str:
        p = _WHITELIST / name
        # Ensure fresh file per test invocation.
        if p.exists() or Path(str(p) + ".npy").exists():
            stem = p.with_name(uuid.uuid4().hex[:8] + "_" + name)
            p = stem
        return str(p)

    return _make


def make_arch_spec(input_features: int = 4, hidden: int = 16, out: int = 3,
                   dropout: float = 0.0) -> dict:
    return {
        "input_features": input_features,
        "layers": [
            {"name": "h1", "type": "dense", "out_features": hidden, "input": "input"},
            {"name": "a1", "type": "relu", "input": "h1"},
            {"name": "d1", "type": "dropout", "p": dropout, "input": "a1"},
            {"name": "out", "type": "dense", "out_features": out, "input": "d1"},
        ],
    }


def create_arch_via_api(client, uid, spec):
    r = client.post(
        "/api/architectures",
        headers=auth(uid),
        json={"name": "a", **spec},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def register_dataset_via_api(client, uid, path, task="classification"):
    r = client.post(
        "/api/datasets",
        headers=auth(uid),
        json={"name": "d", "path": path, "task": task},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]
