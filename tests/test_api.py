"""HTTP API tests using FastAPI's TestClient."""

import numpy as np
from fastapi.testclient import TestClient

from pose_graph.app import app
from pose_graph.synthetic import generate_square_dataset

client = TestClient(app)


def _dataset_payload(seed=10, n_wrong_loops=0, kernel="huber"):
    data = generate_square_dataset(seed=seed, n_wrong_loops=n_wrong_loops)
    edges = data.edges + data.wrong_loop_edges
    return {
        "initial_poses": data.initial_poses.tolist(),
        "edges": [
            {
                "i": e.i,
                "j": e.j,
                "measurement": e.z.tolist(),
                "information": e.omega.tolist(),
            }
            for e in edges
        ],
        "options": {"robust_kernel": kernel, "max_iterations": 50},
    }


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_optimize_endpoint():
    r = client.post("/optimize", json=_dataset_payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"]
    assert body["converged"]
    assert len(body["optimized_poses"]) == 41
    assert len(body["cost_history"]) == body["iterations"] >= 2
    assert body["final_cost"] == body["cost_history"][-1]
    assert body["final_cost"] < body["cost_history"][0]
    assert body["degeneracy"]["is_degenerate"] is False
    # Node 0 must remain fixed.
    assert np.allclose(body["optimized_poses"][0], [0.0, 0.0, 0.0], atol=1e-12)


def test_optimize_with_wrong_loops_robust():
    payload = _dataset_payload(seed=11, n_wrong_loops=4)
    # Robust IRLS reweighting needs more iterations to settle.
    payload["options"]["max_iterations"] = 200
    r = client.post("/optimize", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"]
    assert body["converged"]


def test_invalid_information_matrix_rejected():
    payload = {
        "initial_poses": [[0, 0, 0], [1, 0, 0]],
        "edges": [
            {
                "i": 0,
                "j": 1,
                "measurement": [1, 0, 0],
                "information": [[1, 0, 0], [0, -1, 0], [0, 0, 1]],  # not PD
            }
        ],
    }
    r = client.post("/optimize", json=payload)
    assert r.status_code == 422


def test_out_of_range_node_rejected():
    payload = {
        "initial_poses": [[0, 0, 0], [1, 0, 0]],
        "edges": [
            {
                "i": 0,
                "j": 5,
                "measurement": [1, 0, 0],
                "information": np.eye(3).tolist(),
            }
        ],
    }
    r = client.post("/optimize", json=payload)
    assert r.status_code == 422


def test_malformed_pose_rejected():
    payload = {
        "initial_poses": [[0, 0], [1, 0, 0]],
        "edges": [
            {
                "i": 0,
                "j": 1,
                "measurement": [1, 0, 0],
                "information": np.eye(3).tolist(),
            }
        ],
    }
    r = client.post("/optimize", json=payload)
    assert r.status_code == 422
