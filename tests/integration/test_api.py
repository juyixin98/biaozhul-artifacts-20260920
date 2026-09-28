"""End-to-end API tests over a real ASGI app with an isolated temp database.

These assert concrete results and failure categories, not merely that the
endpoint responds. They also verify run correlation: each response carries a
run_id that resolves in the persistence layer, and the Monte Carlo endpoint
agrees with the planned operating characteristic.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sample_size_planner.api.main import create_app
from sample_size_planner.api.service import PlanningService


@pytest.fixture
def client(tmp_settings) -> TestClient:
    service = PlanningService(tmp_settings)
    return TestClient(create_app(service))


NORMAL_BODY = {
    "alpha": 0.05, "power": 0.8, "direction": "two_sided",
    "allocation_ratio": 1.0, "standardized_effect": 0.8,
}


@pytest.mark.integration
def test_health_reports_versions(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert set(["python", "numpy", "scipy", "fastapi", "pydantic"]) <= body["versions"].keys()


@pytest.mark.integration
def test_plan_normal_returns_concrete_size_and_boundary(client: TestClient) -> None:
    resp = client.post("/plan/normal", json=NORMAL_BODY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["n_per_group0"] == 25 and body["n_total"] == 50
    assert body["achieved_power"] >= 0.8
    assert body["power_at_n_minus_one"] < 0.8
    assert body["failure_category"] == "none"


@pytest.mark.integration
def test_plan_binomial_low_rate_uses_exact_method(client: TestClient) -> None:
    body_in = {"alpha": 0.05, "power": 0.8, "direction": "greater",
               "p0": 0.001, "p1": 0.01}
    resp = client.post("/plan/binomial", json=body_in)
    body = resp.json()
    assert resp.status_code == 200
    assert body["success"] is True
    assert body["method"] == "exact_binomial"
    assert body["achieved_power"] >= 0.8
    assert body["power_at_n_minus_one"] < 0.8


@pytest.mark.integration
def test_plan_normal_zero_effect_is_failure_not_success(client: TestClient) -> None:
    body = {**NORMAL_BODY, "standardized_effect": 0.0}
    resp = client.post("/plan/normal", json=body)
    out = resp.json()
    assert out["success"] is False
    assert out["failure_category"] == "effect_zero"
    assert out["n_total"] is None


@pytest.mark.integration
def test_invalid_power_below_alpha_is_422(client: TestClient) -> None:
    body = {**NORMAL_BODY, "power": 0.02}
    resp = client.post("/plan/normal", json=body)
    assert resp.status_code == 422  # invalid input never masquerades as a plan


@pytest.mark.integration
def test_plan_then_simulate_agree_and_are_run_correlated(client: TestClient) -> None:
    planned = client.post("/plan/normal", json=NORMAL_BODY).json()
    run_id = planned["run_id"]
    sim_body = {
        "family": "normal", "n_per_group0": planned["n_per_group0"],
        "n_per_group1": planned["n_per_group1"], "alpha": 0.05,
        "direction": "two_sided", "standardized_effect": 0.8,
        "replications": 40_000, "seed": 20260927,
        "target_power": planned["achieved_power"],
    }
    sim = client.post("/simulate/power", json=sim_body).json()
    lo, hi = sim["ci95"]
    assert lo <= planned["achieved_power"] <= hi
    assert sim["agrees_with_target"] is True
    assert sim["run_id"] != run_id  # distinct run, each independently traceable

    record = client.get(f"/runs/{run_id}").json()
    assert record["run_id"] == run_id
    assert record["plans"][0]["n_group0"] == 25


@pytest.mark.integration
def test_simulate_requires_family_specific_params(client: TestClient) -> None:
    body = {"family": "binomial", "n_per_group0": 96, "alpha": 0.05,
            "direction": "two_sided", "replications": 5000}
    resp = client.post("/simulate/power", json=body)
    assert resp.status_code == 422


@pytest.mark.integration
def test_interim_endpoint_returns_calibrated_boundary(client: TestClient) -> None:
    body = {"information_times": [0.5, 1.0], "alpha": 0.05,
            "direction": "two_sided", "family": "pocock",
            "drift_at_full_information": 2.8}
    resp = client.post("/interim/plan", json=body)
    assert resp.status_code == 200
    out = resp.json()
    assert out["z_boundaries"][0] == pytest.approx(2.178, abs=5e-3)
    assert out["cumulative_alpha_spent"][-1] == pytest.approx(0.025, abs=1e-3)


@pytest.mark.integration
def test_interim_commitment_violation_is_422_with_category(client: TestClient) -> None:
    body = {"information_times": [0.5, 0.8], "alpha": 0.05,
            "direction": "greater", "family": "pocock",
            "drift_at_full_information": 2.8}
    resp = client.post("/interim/plan", json=body)
    assert resp.status_code == 422
    out = resp.json()
    assert out["failure_category"] == "interim_look_outside_commitment"


@pytest.mark.integration
def test_unknown_run_is_404(client: TestClient) -> None:
    assert client.get("/runs/does-not-exist").status_code == 404
