"""HTTP-level tests using FastAPI's in-process TestClient (real ASGI stack)."""

from __future__ import annotations

import json
import os

import numpy as np
import pytest
from fastapi.testclient import TestClient

from adam_shard.api import create_app

pytestmark = pytest.mark.api


@pytest.fixture
def client(app_config):
    app = create_app(app_config)
    with TestClient(app) as c:
        yield c


def test_health_reports_version_and_storage(client, app_config):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"] == "1.0.0"
    assert body["storage_root"] == app_config.storage_root


def test_train_then_validate_and_verify_parity_flow(client):
    # Train with 2 processes; request_id is correlated through the response.
    r = client.post("/train", json={"world_size": 2, "steps": 2, "request_id": "req-api-2"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["request_id"] == "req-api-2"
    assert body["world_size"] == 2
    assert body["step"] == 2
    assert body["restored_from"] is None
    commit_id = body["commit_id"]

    # Validate + reshard to 3.
    r = client.post("/validate", json={"commit_id": commit_id, "target_world_size": 3})
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["target_shard_sizes"] == [19, 19, 21]
    assert v["source_world_size"] == 2

    # Parity: restore with 3 processes, one step vs independent reference.
    r = client.post(
        "/verify-parity",
        json={"source_commit": commit_id, "target_world_size": 3, "request_id": "req-api-parity"},
    )
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["request_id"] == "req-api-parity"
    assert report["status"] == "pass"
    assert report["failures"] == []
    assert report["uncertainties"] == []
    assert "version" in report and "stage" in report and "location" in report


def test_restore_mismatched_commit_pair_returns_typed_422(client):
    a = client.post("/train", json={"world_size": 2, "steps": 1, "request_id": "req-api-a"}).json()
    b = client.post("/train", json={"world_size": 2, "steps": 1, "request_id": "req-api-b"}).json()
    r = client.post(
        "/train",
        json={
            "world_size": 2,
            "steps": 2,
            "model_commit": a["commit_id"],
            "optim_commit": b["commit_id"],
            "request_id": "req-api-mix",
        },
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["category"] == "commit_mismatch"
    assert detail["commit_id"] in (a["commit_id"], b["commit_id"])


def test_corrupt_shard_returns_digest_mismatch_category(client, app_config):
    commit = client.post(
        "/train", json={"world_size": 2, "steps": 1, "request_id": "req-api-corrupt"}
    ).json()["commit_id"]
    path = os.path.join(app_config.storage_root, commit, "model-r1.npy")
    bad = np.load(path)
    bad[3] += 42.0
    np.save(path, bad)

    r = client.post("/validate", json={"commit_id": commit, "target_world_size": 3})
    assert r.status_code == 422
    assert r.json()["detail"]["category"] == "digest_mismatch"


def test_missing_shard_returns_missing_shard_category(client, app_config):
    commit = client.post(
        "/train", json={"world_size": 2, "steps": 1, "request_id": "req-api-missing"}
    ).json()["commit_id"]
    os.remove(os.path.join(app_config.storage_root, commit, "optim-r0.npz"))
    r = client.post("/validate", json={"commit_id": commit, "target_world_size": 2})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["category"] == "missing_shard"


def test_corrupt_manifest_summary_returns_corrupt_manifest_category(client, app_config):
    commit = client.post(
        "/train", json={"world_size": 2, "steps": 1, "request_id": "req-api-manifest"}
    ).json()["commit_id"]
    path = os.path.join(app_config.storage_root, commit, "manifest.json")
    with open(path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    manifest["step"] = 777  # tamper, leave stale summary digest
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    r = client.post("/validate", json={"commit_id": commit, "target_world_size": 2})
    assert r.status_code == 422
    assert r.json()["detail"]["category"] == "corrupt_manifest"


def test_incomplete_manifest_returns_incomplete_manifest_category(client, app_config):
    commit = client.post(
        "/train", json={"world_size": 3, "steps": 1, "request_id": "req-api-inc"}
    ).json()["commit_id"]
    path = os.path.join(app_config.storage_root, commit, "manifest.json")
    with open(path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    manifest["model_shards"] = [e for e in manifest["model_shards"] if e["rank"] != 2]
    from adam_shard.tensor_types import json_digest

    manifest["manifest_digest"] = json_digest(
        {k: v for k, v in manifest.items() if k != "manifest_digest"}
    )
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    r = client.post("/validate", json={"commit_id": commit, "target_world_size": 3})
    assert r.status_code == 422
    assert r.json()["detail"]["category"] == "incomplete_manifest"


def test_commits_listing_and_unknown_commit_422(client):
    client.post("/train", json={"world_size": 2, "steps": 1, "request_id": "req-api-list"})
    r = client.get("/commits")
    assert r.status_code == 200
    listing = r.json()["commits"]
    assert any(c["loadable"] for c in listing)

    r = client.post("/validate", json={"commit_id": "does-not-exist", "target_world_size": 2})
    assert r.status_code == 422
    assert r.json()["detail"]["category"] == "missing_commit"


def test_bad_world_size_rejected_by_schema(client):
    r = client.post("/train", json={"world_size": 0, "steps": 1})
    assert r.status_code == 422
