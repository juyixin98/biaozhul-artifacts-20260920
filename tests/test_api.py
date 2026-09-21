"""HTTP API tests including SSE replay semantics."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client():
    app = create_app(start_workers=False)
    with TestClient(app) as c:
        yield c


def test_requires_user_header(client):
    r = client.get("/api/v1/jobs/x")
    assert r.status_code == 401


def test_publish_architecture_is_versioned_and_immutable(client, arch_spec):
    r = client.post("/api/v1/architectures",
                    json={"name": "net", "spec": arch_spec})
    assert r.status_code == 201, r.text
    a1 = r.json()
    assert a1["version"] == 1
    r = client.post("/api/v1/architectures",
                    json={"name": "net", "spec": arch_spec})
    a2 = r.json()
    assert a2["version"] == 2
    assert a1["id"] != a2["id"]
    # same spec -> same fingerprint
    assert a1["fingerprint"] == a2["fingerprint"]


def test_bad_spec_returns_422(client):
    r = client.post("/api/v1/architectures", json={
        "name": "bad",
        "spec": {"layers": [{"id": "in", "type": "input"}],
                 "connections": []},
    })
    assert r.status_code == 422


def test_path_traversal_returns_4xx(client):
    r = client.post("/api/v1/datasets", json={
        "feature_path": "../../../etc/passwd", "task": "classification"})
    assert r.status_code in (400, 422)


def test_full_workflow_and_sse_replay(client, sample_data):
    h = {"X-User-Id": "alice"}
    spec = {
        "layers": [
            {"id": "in", "type": "input", "in_features": 2},
            {"id": "h", "type": "dense", "out_features": 8},
            {"id": "a", "type": "relu"},
            {"id": "out", "type": "dense", "out_features": 3},
        ],
        "connections": [["in", "h"], ["h", "a"], ["a", "out"]],
    }
    arch = client.post("/api/v1/architectures",
                       json={"name": "net", "spec": spec}).json()
    ds = client.post("/api/v1/datasets", json={
        "feature_path": str(sample_data["csv"]),
        "task": "classification"}).json()
    job = client.post("/api/v1/jobs", headers=h, json={
        "architecture_id": arch["id"],
        "dataset_id": ds["id"],
        "hyperparams": {"lr": 0.05, "batch_size": 16},
        "epochs": 3, "seed": 3, "val_fraction": 0.25,
    }).json()
    assert job["status"] == "queued"

    # Drive the runner in-process (no background workers in this app).
    from app.db import SessionLocal
    from app.services import queue
    from app.workers.runner import run_job
    db = SessionLocal()
    claimed = queue.claim_next_job(db, "exec-test")
    assert claimed.id == job["id"]
    final = run_job(job["id"], "exec-test", db=db)
    assert final == "completed"
    db.close()

    detail = client.get(f"/api/v1/jobs/{job['id']}", headers=h).json()
    assert detail["status"] == "completed"

    # Event list replay.
    events = client.get(
        f"/api/v1/jobs/{job['id']}/events", headers=h).json()
    assert [e["kind"] for e in events].count("epoch") == 3
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(1, len(seqs) + 1))

    # Resume-after-seq replay returns only newer events (no re-training).
    tail = client.get(
        f"/api/v1/jobs/{job['id']}/events?after=2", headers=h).json()
    assert all(e["seq"] > 2 for e in tail)
    assert len(tail) == len(events) - 2


def test_sse_stream_reconnect_replays(client, sample_data):
    h = {"X-User-Id": "alice"}
    spec = {
        "layers": [
            {"id": "in", "type": "input", "in_features": 2},
            {"id": "o", "type": "dense", "out_features": 3},
        ],
        "connections": [["in", "o"]],
    }
    arch = client.post("/api/v1/architectures",
                       json={"name": "n", "spec": spec}).json()
    ds = client.post("/api/v1/datasets", json={
        "feature_path": str(sample_data["csv"]),
        "task": "classification"}).json()
    job = client.post("/api/v1/jobs", headers=h, json={
        "architecture_id": arch["id"], "dataset_id": ds["id"],
        "hyperparams": {"lr": 0.05, "batch_size": 16},
        "epochs": 2, "seed": 1, "val_fraction": 0.25}).json()

    from app.db import SessionLocal
    from app.services import queue
    from app.workers.runner import run_job
    db = SessionLocal()
    queue.claim_next_job(db, "exec-x")
    run_job(job["id"], "exec-x", db=db)
    db.close()

    # SSE terminates after a terminal event; parse frames.
    with client.stream("GET",
                       f"/api/v1/jobs/{job['id']}/events/stream",
                       headers=h) as resp:
        body = b"".join(resp.iter_bytes()).decode()
    frames = [f for f in body.split("\n\n") if f.strip()]
    assert any("event: completed" in f for f in frames)
    # every frame has a gapless id
    ids = [int(f.split("id: ")[1].split("\n")[0]) for f in frames]
    assert ids == list(range(1, len(ids) + 1))

    # Reconnect with Last-Event-ID: only events after that id are replayed.
    last = ids[-2]
    with client.stream("GET",
                       f"/api/v1/jobs/{job['id']}/events/stream",
                       headers={**h, "Last-Event-ID": str(last)}) as resp:
        body = b"".join(resp.iter_bytes()).decode()
    frames = [f for f in body.split("\n\n") if f.strip()]
    assert len(frames) == 1
    assert "event: completed" in frames[0]


def test_user_isolation(client, sample_data):
    spec = {
        "layers": [
            {"id": "in", "type": "input", "in_features": 2},
            {"id": "o", "type": "dense", "out_features": 3},
        ],
        "connections": [["in", "o"]],
    }
    arch = client.post("/api/v1/architectures",
                       json={"name": "n", "spec": spec}).json()
    ds = client.post("/api/v1/datasets", json={
        "feature_path": str(sample_data["csv"]),
        "task": "classification"}).json()
    job = client.post("/api/v1/jobs", headers={"X-User-Id": "a"}, json={
        "architecture_id": arch["id"], "dataset_id": ds["id"],
        "hyperparams": {"lr": 0.05, "batch_size": 16},
        "epochs": 1, "seed": 1, "val_fraction": 0.25}).json()
    assert client.get(
        f"/api/v1/jobs/{job['id']}", headers={"X-User-Id": "b"}
    ).status_code == 404
