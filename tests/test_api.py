"""API tests using FastAPI's TestClient."""

import numpy as np
from fastapi.testclient import TestClient

from app import app
from pgo.synthetic import build_synthetic_graph

client = TestClient(app)


def _payload(add_outlier=False):
    data = build_synthetic_graph(add_outlier=add_outlier, seed=42)
    return {
        "poses": data.graph.poses.tolist(),
        "edges": [
            {
                "i": e.i,
                "j": e.j,
                "measurement": e.measurement.tolist(),
                "information": e.information.tolist(),
            }
            for e in data.graph.edges
        ],
        "options": {"kernel": "huber", "kernel_delta": 1.0, "max_iterations": 50},
    }


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_optimize_roundtrip():
    r = client.post("/optimize", json=_payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["num_nodes"] == 20
    assert body["num_edges"] == 20
    assert body["final_cost"] < body["initial_cost"]
    assert body["final_gradient_inf_norm"] < 1e-3
    assert body["degenerate"] is False
    assert body["connected_components"] == 1
    assert len(body["poses"]) == 20
    assert all(len(p) == 3 for p in body["poses"])
    assert len(body["edge_costs"]) == 20
    assert len(body["history"]) >= 1
    # gauge: node 0 pinned
    data = build_synthetic_graph(add_outlier=False, seed=42)
    assert np.allclose(body["poses"][0], data.graph.poses[0], atol=1e-9)


def test_optimize_with_outlier_reports_edge_costs():
    r = client.post("/optimize", json=_payload(add_outlier=True))
    assert r.status_code == 200, r.text
    body = r.json()
    costs = np.array(body["edge_costs"])
    assert costs[-1] > 5.0 * np.median(costs[:-1])


def test_bad_information_matrix_422():
    payload = {
        "poses": [[0, 0, 0], [1, 0, 0]],
        "edges": [
            {"i": 0, "j": 1, "measurement": [1, 0, 0],
             "information": [[0, 0, 0], [0, 0, 0], [0, 0, 0]]}
        ],
    }
    r = client.post("/optimize", json=payload)
    assert r.status_code == 422


def test_bad_edge_index_422():
    payload = {
        "poses": [[0, 0, 0], [1, 0, 0]],
        "edges": [
            {"i": 0, "j": 5, "measurement": [1, 0, 0],
             "information": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]}
        ],
    }
    r = client.post("/optimize", json=payload)
    assert r.status_code == 422


def test_malformed_pose_422():
    payload = {"poses": [[0, 0]], "edges": []}
    r = client.post("/optimize", json=payload)
    assert r.status_code == 422
