"""HTTP API tests using FastAPI's TestClient."""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _basic_payload():
    return {
        "observations": [[0, 0], [1.1, 0.9], [2.1, 1.9], [3, 3]],
        "smooth_weight": 1.0,
        "obs_weights": [1.0, 1.0, 1.0, 1.0],
        "corridors": [
            {"A": [], "b": []},
            {"A": [[0, -1]], "b": [0.0]},   # y >= 0
            {"A": [[0, -1]], "b": [0.0]},
            {"A": [], "b": []},
        ],
        "start": [0, 0],
        "end": [3, 3],
    }


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_smooth_optimal():
    r = client.post("/smooth", json=_basic_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "optimal"
    assert body["feasible"] is True
    assert body["path"] is not None
    assert len(body["path"]) == 4
    assert body["path"][0] == [0.0, 0.0]
    assert body["path"][-1] == [3.0, 3.0]
    assert body["objective"] is not None
    for key in ("primal_infeasibility", "stationarity",
                "complementary_slackness", "dual_infeasibility"):
        assert body["residuals"][key] < 1e-6
    # Corridor y >= 0 respected on interior points.
    assert all(p[1] >= -1e-9 for p in body["path"])


def test_smooth_defaults():
    # Corridors / weights / endpoints omitted: unconstrained, endpoints = observations.
    r = client.post("/smooth", json={
        "observations": [[0, 0], [1, 0.2], [2, 0]],
        "smooth_weight": 5.0,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "optimal"
    assert body["path"][0] == [0.0, 0.0]
    assert body["path"][-1] == [2.0, 0.0]
    assert abs(body["path"][1][1]) < 0.2  # pulled toward the straight line


def test_smooth_infeasible_returns_no_path():
    payload = _basic_payload()
    # Corridor 1: x <= 0 and x >= 1 -> empty.
    payload["corridors"][1] = {"A": [[1, 0], [-1, 0]], "b": [0.0, -1.0]}
    r = client.post("/smooth", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "infeasible"
    assert body["feasible"] is False
    assert body["path"] is None
    assert body["objective"] is None
    assert body["residuals"] is None
    assert "corridor 1" in body["message"]


def test_smooth_endpoint_outside_corridor():
    payload = _basic_payload()
    payload["corridors"][0] = {"A": [[1, 0]], "b": [0.5]}  # x <= 0.5
    payload["start"] = [2.0, 0.0]
    r = client.post("/smooth", json=payload)
    body = r.json()
    assert body["status"] == "infeasible"
    assert body["path"] is None


@pytest.mark.parametrize("bad", [
    {"observations": [[0, 0]]},                                   # n < 2
    {"observations": [[0, 0], [1, 1]], "obs_weights": [1.0]},     # wrong length
    {"observations": [[0, 0], [1, 1]], "obs_weights": [1.0, 0.0]},  # non-positive
    {"observations": [[0, 0], [1, 1]],
     "corridors": [{"A": [], "b": []}]},                          # corridors length != n
    {"observations": [[0, 0], [1, 1]],
     "corridors": [{"A": [[1, 0]], "b": [1.0, 2.0]},
                   {"A": [], "b": []}]},                          # A/b mismatch
    {"observations": [[0, 0], [1, 1]], "start": [0, 0, 0]},       # bad start dim
    {"observations": [[0, 0], [1, 1]], "smooth_weight": -1.0},    # negative lambda
])
def test_invalid_requests_rejected(bad):
    r = client.post("/smooth", json=bad)
    assert r.status_code == 422
