"""End-to-end tests through the real FastAPI app (httpx TestClient)."""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from bucket_sync.api import create_app
from bucket_sync.reference import joint_linear_mse, reference_sgd_step
from bucket_sync.training import BIAS_PARAM, WEIGHT_PARAM

from tests.drivers import bucket_segments, local_gradient_sums

pytestmark = pytest.mark.integration

WORKERS = ["w0", "w1", "w2"]
SIZES = (5, 3, 2)


def _submit_all(client, desc, worker_ids, shards, layout):
    for wid, shard in zip(worker_ids, shards):
        grads, n = local_gradient_sums(
            {k: np.asarray(v) for k, v in desc["base_params"].items()}, shard
        )
        for bi, (seg, mask) in bucket_segments(layout, grads).items():
            resp = client.post(
                "/buckets/submit",
                json={
                    "round_id": desc["round_id"],
                    "worker_id": wid,
                    "bucket_index": bi,
                    "segment": seg.tolist(),
                    "present_mask": mask.tolist(),
                    "n_samples": n,
                    "base_token": desc["base_token"],
                },
            )
            assert resp.status_code == 200
            assert resp.json()["accepted"] is True


def test_health_and_request_id_header(coordinator):
    client = TestClient(create_app(coordinator))
    resp = client.get("/health", headers={"x-request-id": "fixed-req-id"})
    assert resp.status_code == 200
    assert resp.json()["generation"] == 0
    assert resp.headers["x-request-id"] == "fixed-req-id"
    # generated id when the client sends none
    resp2 = client.get("/health")
    assert resp2.headers["x-request-id"].startswith("req-")


def test_full_round_over_http_matches_joint_batch(coordinator, layout, unequal_shards, cfg):
    client = TestClient(create_app(coordinator))
    for wid in WORKERS:
        assert client.post(f"/workers/{wid}/register").status_code == 200
    begin = client.post("/rounds/begin", json={"participants": WORKERS})
    assert begin.status_code == 200
    desc = begin.json()
    assert desc["generation"] == 0
    assert len(desc["base_params"]) == 2

    _submit_all(client, desc, WORKERS, unequal_shards, layout)

    commit = client.post("/rounds/commit")
    body = commit.json()
    assert body["outcome"] == "committed"
    assert body["generation_after"] == 1
    assert len(body["bucket_evidence"]) == layout.bucket_count()
    assert body["bucket_evidence"][0]["samples_per_worker"] == list(SIZES)

    state = client.get("/state").json()
    x_joint = np.vstack([s[0] for s in unequal_shards])
    y_joint = np.vstack([s[1] for s in unequal_shards])
    base = {k: np.asarray(v) for k, v in desc["base_params"].items()}
    expected = reference_sgd_step(
        base, joint_linear_mse(base, x_joint, y_joint), cfg.lr
    )
    np.testing.assert_allclose(state["params"][WEIGHT_PARAM], expected[WEIGHT_PARAM], atol=1e-10)
    np.testing.assert_allclose(state["params"][BIAS_PARAM], expected[BIAS_PARAM], atol=1e-10)


def test_rejected_submission_is_data_with_reason_and_record_id(coordinator, layout):
    client = TestClient(create_app(coordinator))
    client.post("/workers/w0/register")
    desc = client.post("/rounds/begin", json={"participants": ["w0"]}).json()
    resp = client.post(
        "/buckets/submit",
        json={
            "round_id": desc["round_id"],
            "worker_id": "w0",
            "bucket_index": 0,
            "segment": [0.0, 0.0],
            "present_mask": [True, True],
            "n_samples": 0,  # invalid
            "base_token": desc["base_token"],
        },
    )
    # HTTP succeeds; the rejection is an explicit, categorized result.
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is False
    assert body["reason"] == "invalid_sample_count"
    assert body["record_id"].startswith("rec-")


def test_unknown_worker_registration_is_404_on_heartbeat(coordinator):
    client = TestClient(create_app(coordinator))
    assert client.post("/workers/ghost/heartbeat").status_code == 404


def test_diagnostics_endpoint_redacts_tensor_values(coordinator, layout, unequal_shards):
    client = TestClient(create_app(coordinator))
    # Exact initial scalars must never appear verbatim in diagnostics.
    secret_scalars = [
        float(v)
    for v in np.asarray(client.get("/state").json()["params"][WEIGHT_PARAM]).reshape(-1)
    ]
    for wid in WORKERS:
        client.post(f"/workers/{wid}/register")
    desc = client.post("/rounds/begin", json={"participants": WORKERS}).json()
    _submit_all(client, desc, WORKERS, unequal_shards, layout)
    client.post("/rounds/commit")
    diag_text = client.get("/diagnostics").text
    for scalar in secret_scalars:
        token = f"{scalar:.16g}".rstrip("0")
        # The full-precision training value must never be logged.
        assert f"{scalar}" not in diag_text
        assert token not in diag_text
    diag = client.get("/diagnostics").json()
    accepted = [e for e in diag["events"] if e["reason"] == "bucket_received"]
    assert accepted
    assert isinstance(accepted[0]["detail"]["segment_norm"], float)


def test_lost_worker_aborts_round_observable_over_http(coordinator, layout):
    client = TestClient(create_app(coordinator))
    client.post("/workers/w0/register")
    client.post("/workers/w1/register")
    desc = client.post("/rounds/begin", json={"participants": ["w0", "w1"]}).json()
    lost = client.post("/workers/w1/lost", json={"reason": "kill_test"})
    assert lost.status_code == 200
    current = client.get("/rounds/current").json()
    assert current["status"] == "aborted"
    commit = client.post("/rounds/commit").json()
    assert commit["outcome"] == "rejected"
    assert commit["reason"] == "round_aborted"
