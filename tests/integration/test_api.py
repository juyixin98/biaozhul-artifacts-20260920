"""Integration tests for the FastAPI surface.

A fresh application (and therefore a fresh RunStore pointed at the tmp DB) is
built per test.  Responses are asserted on concrete statistical values and
explicit failure categories - not merely on the endpoint returning 200.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ssp.api.app import create_app
from ssp.api.service import PlanningService
from ssp.storage import RunStore


@pytest.fixture()
def client(settings):
    service = PlanningService(store=RunStore(settings))
    app = create_app(service)
    with TestClient(app) as test_client:
        yield test_client


class TestHealthAndStartup:
    @pytest.mark.integration
    def test_health_reports_versions_and_passing_selfcheck(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "numpy" in body["versions"] and "scipy" in body["versions"]
        assert body["noncentral_selfcheck"]["worst_size_error"] < 1e-9


class TestNormalEndpoint:
    @pytest.mark.integration
    def test_textbook_z_plan_exact_values(self, client):
        payload = {
            "endpoint": "normal", "alternative": "two_sided", "alpha": 0.05,
            "target_power": 0.8, "effect": 0.5, "effect_scale": "standardized_d",
            "two_sample": False, "known_sigma": True, "mc_trials": 3000,
        }
        resp = client.post("/api/v1/plans/normal", json=payload)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "completed"
        assert body["allocation"]["n0"] == 32
        assert body["achieved_power"] == pytest.approx(0.80743, abs=1e-4)
        check = body["minimal_integer_check"]
        assert check["passes"] is True
        assert check["power_at_total_minus_one"] < 0.8
        assert body["versions"]["scipy"]

    @pytest.mark.integration
    def test_run_id_is_persisted_and_retrievable(self, client):
        payload = {
            "endpoint": "normal", "alternative": "two_sided", "alpha": 0.05,
            "target_power": 0.8, "effect": 0.5, "effect_scale": "standardized_d",
            "two_sample": False, "known_sigma": True, "mc_trials": 1000,
        }
        body = client.post("/api/v1/plans/normal", json=payload).json()
        run_id = body["run_id"]
        fetched = client.get(f"/api/v1/runs/{run_id}")
        assert fetched.status_code == 200
        assert fetched.json()["status"] == "completed"
        assert fetched.json()["result"]["plan"]["run_id"] == run_id

    @pytest.mark.integration
    def test_zero_effect_is_422_with_explicit_category(self, client):
        payload = {
            "endpoint": "normal", "alternative": "two_sided", "alpha": 0.05,
            "target_power": 0.8, "effect": 0.0, "effect_scale": "standardized_d",
            "two_sample": False,
        }
        resp = client.post("/api/v1/plans/normal", json=payload)
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["error_category"] == "validation_error"


class TestBinomialEndpoint:
    @pytest.mark.integration
    def test_low_base_rate_completes_with_exact_method(self, client):
        payload = {
            "endpoint": "binomial", "alternative": "greater", "alpha": 0.05,
            "target_power": 0.8, "p0": 0.01, "effect": 0.03,
            "effect_scale": "proportions", "two_sample": False, "mc_trials": 3000,
        }
        resp = client.post("/api/v1/plans/binomial", json=payload)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["method"] == "binomial_exact_one_sample"
        assert body["allocation"]["n0"] == 301
        assert body["warnings"]

    @pytest.mark.integration
    def test_forced_asymptotic_low_rate_is_409_not_success(self, client):
        payload = {
            "endpoint": "binomial", "alternative": "greater", "alpha": 0.05,
            "target_power": 0.8, "p0": 0.01, "effect": 0.03,
            "effect_scale": "proportions", "two_sample": False,
            "method_preference": "asymptotic",
        }
        resp = client.post("/api/v1/plans/binomial", json=payload)
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["error_category"] == "approximation_invalid"
        assert detail["status"] == "failed"

    @pytest.mark.integration
    def test_direction_effect_conflict_is_422(self, client):
        payload = {
            "endpoint": "binomial", "alternative": "less", "alpha": 0.05,
            "target_power": 0.8, "p0": 0.2, "effect": 0.3,
            "effect_scale": "proportions", "two_sample": False,
        }
        resp = client.post("/api/v1/plans/binomial", json=payload)
        assert resp.status_code == 422
        assert resp.json()["detail"]["error_category"] == "validation_error"

    @pytest.mark.integration
    def test_interim_looks_above_one_is_rejected(self, client):
        payload = {
            "endpoint": "binomial", "alternative": "greater", "alpha": 0.05,
            "target_power": 0.8, "p0": 0.2, "effect": 0.3,
            "effect_scale": "proportions", "two_sample": False, "interim_looks": 4,
        }
        resp = client.post("/api/v1/plans/binomial", json=payload)
        assert resp.status_code == 422


class TestRunLookup:
    @pytest.mark.integration
    def test_unknown_run_is_404(self, client):
        assert client.get("/api/v1/runs/does-not-exist").status_code == 404

    @pytest.mark.integration
    def test_list_runs_returns_most_recent_first(self, client):
        payload = {
            "endpoint": "normal", "alternative": "greater", "alpha": 0.05,
            "target_power": 0.8, "effect": 0.5, "effect_scale": "standardized_d",
            "two_sample": False, "known_sigma": True, "mc_trials": 500,
        }
        first = client.post("/api/v1/plans/normal", json=payload).json()["run_id"]
        second = client.post("/api/v1/plans/normal", json=payload).json()["run_id"]
        runs = client.get("/api/v1/runs").json()["runs"]
        ids = [r["run_id"] for r in runs[:2]]
        assert ids == [second, first]
