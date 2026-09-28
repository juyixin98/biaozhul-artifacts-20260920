"""HTTP-level tests: request-id correlation, success payload and typed errors."""
from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from adam_shards.api import create_app
from adam_shards.config import AppConfig
from adam_shards.adam import AdamConfig


@pytest.fixture
def client(tmp_path):
    cfg = AppConfig(
        checkpoint_dir=str(tmp_path / "ckpt"),
        fixture_path=str(tmp_path / "fixture.npz"),
        seed=20260927, layer_dims=(3, 4, 2), batch_size=8, train_steps=4,
        adam=AdamConfig(lr=0.05), save_world_size=2, restore_world_size=3,
        tight_tol=1e-12, loose_tol=1e-8,
    )
    return TestClient(create_app(cfg))


def test_health_echoes_client_request_id(client):
    rid = "req-health-123"
    resp = client.get("/health", headers={"X-Request-ID": rid})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["request_id"] == rid


def test_pipeline_endpoint_reports_pass_with_request_id(client):
    rid = "req-api-pipeline"
    resp = client.post(
        "/verify/pipeline",
        headers={"X-Request-ID": rid},
        json={"save_world_size": 2, "restore_world_size": 3,
              "train_steps": 4, "tol": 1e-12},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["request_id"] == rid
    assert body["ok"] is True
    assert body["passed"] is True
    assert body["save_world_size"] == 2
    assert body["restore_world_size"] == 3
    assert body["step_after_restore"] == 5
    assert body["parameter_comparison"]["failures"] == []


def test_pipeline_generates_request_id_when_absent(client):
    resp = client.post(
        "/verify/pipeline",
        json={"save_world_size": 1, "restore_world_size": 1},
    )
    assert resp.status_code == 200
    body = resp.json()
    # Same-count restore surfaces an explicit uncertainty, not a failure.
    assert body["passed"] is True
    uncert = body["parameter_comparison"]["uncertainties"]
    assert any("same process count" in u for u in uncert)
    assert body["request_id"].startswith("req-")


def test_reshard_returns_typed_error_for_missing_source(tmp_path, client):
    rid = "req-api-missing"
    resp = client.post(
        "/checkpoints/reshard",
        headers={"X-Request-ID": rid},
        json={"src_dir": str(tmp_path / "does-not-exist"),
              "dst_dir": str(tmp_path / "out"), "new_world_size": 3},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["ok"] is False
    assert body["category"] == "missing_shard"
    assert body["request_id"] == rid
    assert "manifest not found" in body["error"]


def test_validation_error_is_structured(client):
    resp = client.post(
        "/verify/pipeline",
        json={"save_world_size": 0, "restore_world_size": 3},
    )
    assert resp.status_code == 422  # pydantic request validation
