"""HTTP 接口测试（FastAPI TestClient）。"""

import numpy as np
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_simulate_returns_problem_and_ground_truth():
    r = client.post("/v1/simulate", json={"n_cameras": 4, "n_points": 15, "seed": 7})
    assert r.status_code == 200
    body = r.json()
    assert "problem" in body and "ground_truth" in body
    assert len(body["problem"]["cameras"]) == 4
    assert len(body["problem"]["observations"]) > 0


def test_solve_roundtrip():
    sim = client.post(
        "/v1/simulate", json={"n_cameras": 4, "n_points": 15, "seed": 8}
    ).json()
    r = client.post(
        "/v1/solve",
        json={"problem": sim["problem"], "options": {"solver": "schur", "max_iterations": 30}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["converged"]
    assert body["cost_history"][0] > body["final_cost"]
    assert body["diagnostics"]["num_negative_depth_excluded"] >= 1
    assert len(body["diagnostics"]["under_observed_points"]) >= 1


def test_solve_simulated_schur_and_full_agree():
    payload = {"n_cameras": 4, "n_points": 15, "seed": 9, "n_behind_camera": 0, "n_under_observed": 0}
    # 一键接口冒烟
    r = client.post("/v1/solve_simulated", json={"scene": payload, "options": {"solver": "schur"}})
    assert r.status_code == 200
    assert r.json()["converged"]
    # 同一场景下两种求解器结果一致
    sim = client.post("/v1/simulate", json=payload).json()
    costs = {}
    for solver in ("schur", "full"):
        body = client.post(
            "/v1/solve",
            json={"problem": sim["problem"], "options": {"solver": solver}},
        ).json()
        costs[solver] = body["final_cost"]
    np.testing.assert_allclose(costs["schur"], costs["full"], rtol=1e-9)


def test_solve_rejects_bad_problem():
    r = client.post("/v1/solve", json={"problem": {"intrinsics": {"fx": 1, "fy": 1, "cx": 0, "cy": 0}, "cameras": [], "points": [[1, 2]], "observations": []}})
    assert r.status_code == 400
