"""Integration tests for the FastAPI service via an in-process client."""
from __future__ import annotations

import numpy as np
from fastapi.testclient import TestClient

from tensor_backend.api.app import app
from tensor_backend.validation import run_validation_suite

client = TestClient(app)


def test_health_reports_version():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["version"]
    assert r.headers["x-service-version"]


def test_request_id_is_echoed():
    r = client.get("/health", headers={"x-request-id": "req-fixed-1"})
    assert r.headers["x-request-id"] == "req-fixed-1"
    assert r.json()["service"]


def test_graph_transpose_reshape_end_to_end():
    plan = {
        "constants": {"x": np.arange(12).reshape(3, 4).tolist()},
        "steps": [
            {"op": "transpose", "out": "xt", "src": "x"},
            {"op": "reshape", "out": "xf", "src": "xt", "shape": [12], "allow_copy": True},
        ],
    }
    r = client.post("/graphs", json=plan, headers={"x-request-id": "req-g-1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["request_id"] == "req-g-1"
    assert body["tensors"]["xt"]["strides"] == [1, 4]
    assert body["tensors"]["xf"]["storage_id"] != body["tensors"]["xt"]["storage_id"]
    # The trace explicitly distinguishes aliased views from copies.
    by_op = {n["opcode"]: n for n in body["trace"]}
    assert by_op["transpose"]["aliases_storage"] is True
    assert by_op["reshape"]["copied"] is True


def test_graph_overlap_failure_is_classified():
    plan = {
        "constants": {"v": list(range(6))},
        "steps": [
            {"op": "slice", "out": "dst", "src": "v", "start": [0], "stop": [4]},
            {"op": "slice", "out": "src", "src": "v", "start": [2], "stop": [6]},
            {"op": "add", "out": "ignored", "a": "src", "b": "src", "into": "dst"},
        ],
    }
    r = client.post("/graphs", json=plan)
    assert r.status_code == 422
    body = r.json()
    assert body["ok"] is False
    assert body["failure_category"] == "overlapping_write"
    assert body["request_id"]


def test_graph_overlap_temp_policy_succeeds():
    plan = {
        "constants": {"v": list(range(6))},
        "steps": [
            {"op": "slice", "out": "dst", "src": "v", "start": [0], "stop": [4]},
            {"op": "slice", "out": "src", "src": "v", "start": [2], "stop": [6]},
            {"op": "assign", "dst": "dst", "src": "src", "overlap_policy": "temp"},
        ],
    }
    r = client.post("/graphs", json=plan)
    assert r.status_code == 200, r.text
    assert r.json()["tensors"]["dst"]["values"] == [2.0, 3.0, 4.0, 5.0]


def test_reshape_check_endpoint_reports_copy():
    r = client.post("/reshape/check", json={
        "shape": [4, 3], "strides": [1, 4], "new_shape": [12],
    })
    assert r.status_code == 200
    assert r.json()["zero_copy"] is False
    assert "copy" in r.json()["reason"]


def test_reshape_check_oob_category():
    r = client.post("/reshape/check", json={
        "shape": [3, 3], "strides": [3, 1], "new_shape": [9],
        "storage_size": 4,
    })
    assert r.status_code == 422
    assert r.json()["failure_category"] == "out_of_bounds"


def test_validate_endpoint_all_pass():
    r = client.post("/validate")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["failed"] == 0
    assert body["summary"]["errors"] == 0
    assert body["oracle"] == "numpy"
    assert body["failures"] == []
    assert body["uncertainties"] == []


def test_train_linear_endpoint_converges():
    rng = np.random.default_rng(0)
    x = rng.normal(size=(64, 2)).tolist()
    w = np.array([[2.0], [-1.0]])
    y = (np.array(x) @ w + 0.5).tolist()
    r = client.post("/train/linear", json={
        "x": x, "y": y, "steps": 60, "learning_rate": 0.05,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["final_version"] == 60
    assert body["losses"][-1] < body["losses"][0]
    learned = np.array(body["state"]["parameters"][0]["values"]).reshape(-1)
    np.testing.assert_allclose(learned, [2.0, -1.0], atol=0.05)


def test_validation_suite_matches_standalone():
    # The HTTP-exposed suite must be the same one runnable in-process.
    report = run_validation_suite()
    assert report["summary"]["passed"] == report["summary"]["total"]
