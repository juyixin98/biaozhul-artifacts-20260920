"""End-to-end HTTP API tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.config import Settings
from app.fixtures import get_fixture


@pytest.fixture
def client(tmp_path):
    settings = Settings(database_path=str(tmp_path / "test.db"))
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client
    app.state.evidence_store.close()


def test_health_reports_version(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["version"]


def test_solve_sat_persists_and_is_retrievable(client) -> None:
    payload = {
        "model": get_fixture("multiple_solutions"),
        "max_nodes": 1000,
        "max_backtracks": 1000,
    }
    response = client.post("/api/solve", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "sat"
    assert body["solution"] in [
        {"x": 1, "y": 2, "z": 3},
        {"x": 1, "y": 3, "z": 2},
        {"x": 2, "y": 1, "z": 3},
        {"x": 2, "y": 3, "z": 1},
        {"x": 3, "y": 1, "z": 2},
        {"x": 3, "y": 2, "z": 1},
    ]
    assert body["run_id"]
    assert body["failure"] is None

    detail = client.get(f"/api/runs/{body['run_id']}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "sat"
    listing = client.get("/api/runs")
    assert any(row["run_id"] == body["run_id"] for row in listing.json())


def test_solve_unsat_reports_failure_category(client) -> None:
    response = client.post(
        "/api/solve", json={"model": get_fixture("hall_conflict")}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unsat"
    assert body["solution"] is None
    assert body["failure"]["kind"] == "hall_violation"


def test_solve_unknown_does_not_fake_success(client) -> None:
    response = client.post(
        "/api/solve",
        json={"model": get_fixture("queens_8"), "max_nodes": 1},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unknown"
    assert body["solution"] is None
    assert body["failure"]["kind"] == "budget"


def test_reasons_endpoint_evidence(client) -> None:
    response = client.post(
        "/api/solve", json={"model": get_fixture("forced_chain")}
    )
    run_id = response.json()["run_id"]
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["reasons"], "expected recorded pruning reasons"
    for reason in detail["reasons"]:
        assert reason["constraint"]
        assert reason["kind"] in {"binary_support", "alldifferent_matching"}
        assert reason["detail"]


def test_fixtures_listed_and_served(client) -> None:
    listing = client.get("/api/fixtures")
    assert listing.status_code == 200
    names = listing.json()["fixtures"]
    assert "hall_conflict" in names
    payload = client.get("/api/fixtures/hall_conflict")
    assert payload.status_code == 200
    assert payload.json()["name"] == "hall_conflict"
    missing = client.get("/api/fixtures/does_not_exist")
    assert missing.status_code == 404


def test_invalid_model_returns_422_not_200(client) -> None:
    bad = {
        "name": "bad",
        "domains": {"x": []},
        "binary_constraints": [],
        "all_different": [],
    }
    response = client.post("/api/solve", json={"model": bad})
    assert response.status_code == 422


def test_unknown_run_404(client) -> None:
    response = client.get("/api/runs/nope")
    assert response.status_code == 404
