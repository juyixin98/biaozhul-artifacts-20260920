"""FastAPI service: envelopes, error semantics, end-to-end skip via HTTP."""

import pytest

from fastapi.testclient import TestClient

from amptrain.api import app, registry


@pytest.fixture
def client():
    # Fresh registry state per test so run ids do not leak between cases.
    registry._runs.clear()
    registry._fixture_specs.clear()
    return TestClient(app)


def _create(client, **overrides):
    body = {
        "model": {"in_dim": 4, "hidden_dim": 6, "out_dim": 1, "activation": "relu"},
        "precision": {"lowp_dtype": "float16", "master_dtype": "float32"},
        "scaler": {"init_scale": 128.0, "backoff_factor": 0.5, "growth_interval": 2000},
        "accumulation": {"micro_batches": 1},
        "optimizer": {"lr": 0.01, "momentum": 0.9},
        "data": {"n_features": 4, "noise_std": 0.0, "seed": 77},
        "seed": 42,
        "batch_size": 8,
        "n_samples": 64,
        "amplification": 1.0,
    }
    body.update(overrides)
    resp = client.post("/runs", json=body)
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["success"] is True
    return payload["run"]["run_id"]


def test_healthz_reports_versions(client):
    payload = client.get("/healthz").json()
    assert payload["success"] is True
    assert payload["status"] == "ok"
    assert set(payload["versions"]) == {"amptrain", "python", "numpy"}


def test_create_run_initial_scale_and_counters(client):
    run_id = _create(client)
    run = client.get(f"/runs/{run_id}").json()["run"]
    assert run["scale"] == 128.0
    assert run["committed_steps"] == 0
    assert run["skipped_windows"] == 0


def test_auto_windows_commit_then_amplified_run_skips(client):
    run_id = _create(client)
    ok = client.post(f"/runs/{run_id}/windows/auto", json={"n_windows": 2}).json()
    assert all(o["status"] == "committed" for o in ok["outcomes"])

    # New run whose fixture inputs are amplified enough to overflow fp16.
    bad_run = _create(client, amplification=1000.0)
    resp = client.post(f"/runs/{bad_run}/windows/auto", json={"n_windows": 1}).json()
    outcome = resp["outcomes"][0]
    assert outcome["status"] == "skipped"
    assert outcome["scale_after"] == 64.0
    assert outcome["overflow_stage"] == "scaled_loss"
    assert resp["run"]["skipped_windows"] == 1
    assert resp["run"]["committed_steps"] == 0


def test_unknown_run_returns_error_envelope_not_success(client):
    resp = client.get("/runs/nope")
    assert resp.status_code == 404
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "RUN_NOT_FOUND"


def test_invalid_config_is_400_with_code(client):
    resp = client.post("/runs", json={"scaler": {"init_scale": -1}})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "CONFIG_INVALID"


def test_custom_window_wrong_batch_count_is_rejected(client):
    run_id = _create(client)
    x = [[1.0, 0.0, 0.0, 0.0]] * 8
    y = [[1.0]] * 8
    resp = client.post(
        f"/runs/{run_id}/windows/custom",
        json={"batches": [{"x": x, "y": y}, {"x": x, "y": y}]},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INPUT_INVALID"


def test_custom_amplified_window_skips_and_normal_window_commits(client):
    run_id = _create(client)
    big = 1000.0
    x = [[big, 0.0, 0.0, 0.0]] * 8
    y = [[big]] * 8
    skipped = client.post(
        f"/runs/{run_id}/windows/custom", json={"batches": [{"x": x, "y": y}]}
    ).json()["outcome"]
    assert skipped["status"] == "skipped"

    x_ok = [[0.01, 0.01, 0.01, 0.01]] * 8
    committed = client.post(
        f"/runs/{run_id}/windows/custom", json={"batches": [{"x": x_ok, "y": [[0.1]] * 8}]}
    ).json()["outcome"]
    assert committed["status"] == "committed"
    assert committed["committed_steps"] == 1


def test_events_endpoint_correlates_run(client):
    run_id = _create(client)
    client.post(f"/runs/{run_id}/windows/auto", json={"n_windows": 1})
    events = client.get(f"/runs/{run_id}/events").json()
    assert events["count"] >= 2  # run_created + window_committed
    assert all(e["run_id"] == run_id for e in events["events"])
    assert all("version" in e for e in events["events"])


def test_checkpoint_save_and_load_roundtrip_via_api(client, tmp_path):
    run_id = _create(client)
    client.post(f"/runs/{run_id}/windows/auto", json={"n_windows": 2})
    save = client.post(
        f"/runs/{run_id}/checkpoint", json={"directory": str(tmp_path / "ckpt")}
    ).json()
    assert save["success"] is True
    assert save["checkpoint"]["scale"] == 128.0

    loaded = client.post(
        "/checkpoints/load",
        json={"directory": str(tmp_path / "ckpt"), "attach_fixture": False},
    )
    assert loaded.status_code == 201, loaded.text
    restored_id = loaded.json()["run"]["run_id"]
    assert restored_id == run_id
    assert loaded.json()["run"]["committed_steps"] == 2
