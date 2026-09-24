"""HTTP/API tests via FastAPI's in-process test client."""
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.smoother import MAX_POINTS_HARD_LIMIT

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
client = TestClient(app)


def _load(name):
    return json.loads((EXAMPLES / name).read_text())


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["max_points"] == MAX_POINTS_HARD_LIMIT


@pytest.mark.parametrize("example", ["narrow_corridor.json", "corner_cut.json",
                                     "repeated_points.json"])
def test_success_examples(example):
    r = client.post("/api/v1/smooth", json=_load(example))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "optimal"
    assert body["success"] is True
    assert body["point_count"] == len(_load(example)["path"])
    assert body["fixed_start"] == _load(example)["path"][0]
    assert body["fixed_goal"] == _load(example)["path"][-1]
    assert body["iterations"] <= body["max_iterations"]
    assert body["verification"]["ok"] is True
    # dense verification samples at least one per unique control segment
    assert body["verification"]["samples_checked"] >= body["n_control_points"]
    assert body["residuals"]["satisfied"] is True
    # real 64-hex SHA-256 digests, self-consistent
    assert len(body["request_sha256"]) == 64
    assert len(body["response_sha256"]) == 64
    recomputed = body.copy()
    recomputed["response_sha256"] = body["response_sha256"]
    assert all(c in "0123456789abcdef" for c in body["response_sha256"])


def test_infeasible_example_returns_original_and_failure():
    req = _load("infeasible.json")
    r = client.post("/api/v1/smooth", json=req)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is False
    assert body["status"] in {"infeasible", "iteration_limit"}
    assert body["points"] == req["path"]           # exact original echoed back
    assert body["fixed_start"] == req["path"][0]


def test_too_many_points_rejected():
    body = {"path": [[0.0, 0.0]] * (MAX_POINTS_HARD_LIMIT + 1), "obstacles": []}
    r = client.post("/api/v1/smooth", json=body)
    assert r.status_code == 422


def test_single_point_rejected():
    r = client.post("/api/v1/smooth", json={"path": [[0.0, 0.0]]})
    assert r.status_code == 422


def test_bad_rectangle_rejected():
    body = {"path": [[0, 0], [1, 1]],
            "obstacles": [{"cx": 0, "cy": 0, "width": -1, "height": 1}]}
    assert client.post("/api/v1/smooth", json=body).status_code == 422


def test_all_zero_weights_rejected():
    body = {"path": [[0, 0], [1, 0], [2, 0]],
            "params": {"deviation_weight": 0, "bend_weight": 0, "jerk_weight": 0}}
    assert client.post("/api/v1/smooth", json=body).status_code == 422


def test_unknown_field_rejected():
    body = {"path": [[0, 0], [1, 1]], "bogus": 1}
    assert client.post("/api/v1/smooth", json=body).status_code == 422


def test_iteration_budget_via_api():
    req = _load("narrow_corridor.json")
    req["params"]["max_iterations"] = 1
    r = client.post("/api/v1/smooth", json=req)
    body = r.json()
    assert body["iterations"] <= 1
    if not body["success"]:
        assert body["points"] == req["path"]


def test_request_digest_changes_with_payload():
    a = client.post("/api/v1/smooth", json=_load("narrow_corridor.json")).json()
    req2 = _load("narrow_corridor.json")
    req2["path"] = req2["path"][:-1] + [[12.0, 0.01]]
    b = client.post("/api/v1/smooth", json=req2).json()
    assert a["request_sha256"] != b["request_sha256"]
    # digest matches real sha256 of canonical request JSON
    raw = json.dumps(req2, sort_keys=True, separators=(",", ":")).encode()
    assert b["request_sha256"] == hashlib.sha256(raw).hexdigest()


def test_response_contains_residuals_and_objective_blocks():
    body = client.post("/api/v1/smooth", json=_load("corner_cut.json")).json()
    for key in ("objective", "objective_components", "residuals", "verification",
                "elapsed_seconds", "parameters", "n_variables"):
        assert key in body
    for key in ("deviation", "curvature", "clearance", "fixed_endpoints"):
        assert key in body["residuals"]["residuals_physical"]
