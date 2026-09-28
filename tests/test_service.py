"""Service tests: HTTP semantics over a real ASGI app (TestClient)."""

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Isolate checkpoint/log dirs per test before the app module is imported.
    monkeypatch.setenv("MPTRAINER_CKPT_DIR", str(tmp_path / "ckpt"))
    monkeypatch.setenv("MPTRAINER_LOG_DIR", str(tmp_path / "logs"))
    import importlib
    import service.app as app_module

    importlib.reload(app_module)
    app_module.RUNS.clear()
    return TestClient(app_module.app)


def test_health_and_version(client):
    assert client.get("/health").json() == {"status": "ok"}
    versions = client.get("/version").json()
    assert versions["numpy"] and versions["mptrainer"]


def test_create_run_and_step(client):
    resp = client.post("/runs", json={"config": {"accum_steps": 2, "growth_interval": 4}})
    assert resp.status_code == 201
    run_id = resp.json()["run_id"]

    resp = client.post(f"/runs/{run_id}/steps", json={"n": 3, "batch_size": 8})
    assert resp.status_code == 200
    records = resp.json()["records"]
    assert [r["decision"] for r in records] == [
        "accumulating", "committed", "accumulating",
    ]
    state = client.get(f"/runs/{run_id}/state").json()
    assert state["optimizer_step"] == 1
    assert state["micro_step"] == 3


def test_overflow_is_data_not_error(client):
    run_id = client.post("/runs", json={}).json()["run_id"]
    resp = client.post(f"/runs/{run_id}/steps", json={"n": 1, "amplify": 1.0e4})
    assert resp.status_code == 200
    rec = resp.json()["records"][0]
    assert rec["decision"] == "skipped_overflow"
    assert rec["overflow"] is True
    assert rec["scale_after"] == rec["scale_before"] * 0.5
    assert resp.json()["state"]["optimizer_step"] == 0


def test_unknown_run_is_404(client):
    assert client.get("/runs/nope/state").status_code == 404
    assert client.post("/runs/nope/steps", json={"n": 1}).status_code == 404


def test_invalid_config_is_422(client):
    resp = client.post("/runs", json={"config": {"accum_steps": 0}})
    assert resp.status_code == 422
    assert "accum_steps" in resp.json()["detail"]
    resp = client.post("/runs", json={"config": {"low_dtype": "float8"}})
    assert resp.status_code == 422


def test_invalid_step_request_is_422(client):
    run_id = client.post("/runs", json={}).json()["run_id"]
    assert client.post(f"/runs/{run_id}/steps", json={"n": 0}).status_code == 422
    assert client.post(f"/runs/{run_id}/steps", json={"amplify": -1}).status_code == 422


def test_duplicate_run_id_is_409(client):
    resp = client.post("/runs", json={"run_id": "fixed"})
    assert resp.status_code == 201
    assert client.post("/runs", json={"run_id": "fixed"}).status_code == 409


def test_checkpoint_restore_roundtrip_via_api(client):
    run_id = client.post("/runs", json={}).json()["run_id"]
    client.post(f"/runs/{run_id}/steps", json={"n": 1, "amplify": 1.0e4})  # 1024->512
    client.post(f"/runs/{run_id}/steps", json={"n": 1})                    # commit
    before = client.get(f"/runs/{run_id}/state").json()
    assert before["scale"] == 512.0 and before["optimizer_step"] == 1

    resp = client.post(f"/runs/{run_id}/checkpoint")
    assert resp.status_code == 200

    # Mutate state, then restore: scale and counters must come back.
    client.post(f"/runs/{run_id}/steps", json={"n": 1, "amplify": 1.0e4})
    assert client.get(f"/runs/{run_id}/state").json()["scale"] == 256.0
    resp = client.post(f"/runs/{run_id}/restore")
    assert resp.status_code == 200
    restored = resp.json()["state"]
    assert restored["scale"] == 512.0
    assert restored["optimizer_step"] == 1
    assert restored["micro_step"] == 2


def test_restore_without_checkpoint_is_404(client):
    run_id = client.post("/runs", json={}).json()["run_id"]
    assert client.post(f"/runs/{run_id}/restore").status_code == 404
