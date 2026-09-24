"""HTTP API tests via FastAPI's in-process test client."""

import pytest
from fastapi.testclient import TestClient

from app.api import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["osqp"]


def test_matrices_endpoint_exposes_model():
    body = client.get("/matrices").json()
    assert body["A"][0][1] == pytest.approx(0.1)
    assert body["B"][1][0] == pytest.approx(0.1)
    assert len(body["Qf"]) == 2


def test_solve_ok():
    r = client.post(
        "/solve",
        json={"state": [1.5, 0.0], "reference": [0.0] * 20},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "solved"
    assert body["fallback"] is False
    assert abs(body["control"]) <= 1.0 + 1e-9
    assert body["residuals"]["dynamics"] < 1e-6
    assert body["config"]["horizon"] == 20


def test_solve_infeasible_initial_state_is_200_with_fallback():
    r = client.post("/solve", json={"state": [0.0, 3.0], "reference": [0.0]})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "infeasible_initial_state"
    assert body["fallback"] is True
    assert body["control"] == -1.0
    assert "v_max" in body["reason"]


def test_solve_with_config_override():
    r = client.post(
        "/solve",
        json={
            "state": [0.0, 0.0],
            "reference": [1.0] * 10,
            "config": {"horizon": 10, "v_max": 1.0, "u_max": 0.5, "dt": 0.05},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["config"]["horizon"] == 10
    assert body["config"]["u_max"] == 0.5
    assert abs(body["control"]) <= 0.5 + 1e-12
    assert len(body["predicted_controls"]) == 10


def test_validation_rejects_bad_state():
    r = client.post("/solve", json={"state": [1.0], "reference": [0.0]})
    assert r.status_code == 422
    r = client.post(
        "/solve", json={"state": [1.0, 1.0], "reference": [], "u_prev": 0.0}
    )
    assert r.status_code == 422
    # NaN is rejected by the finite-number validator at the schema layer
    import pytest
    from pydantic import ValidationError

    from app.schemas import SolveRequest

    with pytest.raises(ValidationError):
        SolveRequest(state=[1.0, float("nan")], reference=[0.0])


def test_simulate_endpoint():
    ref = [0.0] * 20 + [2.0] * 40
    r = client.post(
        "/simulate",
        json={
            "initial_state": [0.0, 0.0],
            "reference": ref,
            "steps": 60,
            "disturbance_amplitude": 0.2,
            "disturbance_seed": 1,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["steps"] == 60
    assert body["n_fallback"] == 0
    assert len(body["records"]) == 60
    assert abs(body["final_state"][0] - 2.0) < 0.1
    for rec in body["records"]:
        assert rec["velocity_violation"] == 0.0
        assert rec["input_violation"] == 0.0
