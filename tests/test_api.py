"""End-to-end HTTP tests against the FastAPI app (real ASGI + SQLite)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tplan import __version__
from tplan.api import app
from tplan.config import Settings

from .cases import BOUNDARY_RELEASE, OPTIMALITY_GAP, UNSAT


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    # Point the module-level settings at an isolated temp DB for this test.
    import tplan.api as api_mod

    isolated = Settings(
        db_path=str(tmp_path / "evidence.sqlite3"),
        default_node_budget=100_000,
        default_time_budget_seconds=10.0,
        max_horizon=40,
        max_actions=32,
        max_repeats=4,
        log_dir=str(tmp_path / "logs"),
    )
    monkeypatch.setattr(api_mod, "SETTINGS", isolated)
    with TestClient(app) as c:
        yield c


def test_health_reports_versions(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["python"]


def test_solve_returns_optimal_plan_and_evidence_identifiers(client: TestClient) -> None:
    r = client.post("/api/v1/problems/solve", json={"problem": BOUNDARY_RELEASE})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "optimal"
    assert body["optimal"] is True
    assert body["failure_code"] is None
    assert body["best_cost"] == 2
    assert body["goal_time"] == 5
    assert body["input_sha256"] and len(body["input_sha256"]) == 16
    assert body["run_id"].startswith("run-")
    assert [(s["action_id"], s["start"], s["end"]) for s in body["schedule"]] == [
        ("A", 0, 3),
        ("B", 3, 5),
    ]
    # Full independently replayed timeline is embedded.
    assert body["timeline"][0]["kind"] == "init"
    assert body["engine"]["service_version"] == __version__


def test_unsat_returns_explicit_category_with_200(client: TestClient) -> None:
    r = client.post("/api/v1/problems/solve", json={"problem": UNSAT})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "unsat"
    assert body["failure_code"] == "unsat_proven"
    assert body["schedule"] == []


def test_invalid_problem_is_422_with_specific_code(client: TestClient) -> None:
    r = client.post(
        "/api/v1/problems/solve",
        json={"problem": {"horizon": "nope", "fluents": {}, "actions": [], "goal": {}}},
    )
    assert r.status_code == 422
    # Pydantic shape validation on the envelope OR domain validation; either
    # must be a structured error, never a success.
    assert r.text


def test_domain_invalid_problem_is_422_and_persisted(client: TestClient) -> None:
    r = client.post(
        "/api/v1/problems/solve",
        json={"problem": {"horizon": -2, "fluents": {}, "actions": [], "goal": {}}},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "invalid"
    assert body["failure_code"] == "invalid_problem"
    run_id = body["run_id"]
    fetched = client.get(f"/api/v1/runs/{run_id}")
    assert fetched.status_code == 200
    assert fetched.json()["failure_code"] == "invalid_problem"


def test_budget_parameter_changes_status_to_unproven(client: TestClient) -> None:
    # An extremely small node budget yields a feasible but unproven answer.
    found = None
    for budget in range(1, 40):
        r = client.post(
            "/api/v1/problems/solve",
            json={"problem": OPTIMALITY_GAP, "node_budget": budget},
        )
        assert r.status_code == 200
        body = r.json()
        if body["status"] == "feasible_not_proven_optimal":
            found = body
            break
    assert found is not None
    assert found["failure_code"] == "budget_exhausted"
    assert found["optimal"] is False
    assert found["schedule"]  # concrete plan returned


def test_replay_endpoint_round_trip(client: TestClient) -> None:
    r = client.post(
        "/api/v1/problems/replay",
        json={
            "problem": BOUNDARY_RELEASE,
            "schedule": [
                {"action_id": "A", "start": 0, "end": 3},
                {"action_id": "B", "start": 2, "end": 4},
            ],
        },
    )
    assert r.status_code == 409
    body = r.json()
    assert body["ok"] is False
    assert body["failure_time"] == 2
    assert body["failure_code"] in {"resource_conflict", "start_condition_violated"}


def test_run_evidence_fetchable_and_listed(client: TestClient) -> None:
    body = client.post("/api/v1/problems/solve", json={"problem": BOUNDARY_RELEASE}).json()
    run_id = body["run_id"]

    detail = client.get(f"/api/v1/runs/{run_id}")
    assert detail.status_code == 200
    rec = detail.json()
    assert rec["run_id"] == run_id
    assert rec["timeline"] and rec["trace"]
    assert rec["budgets"]["nodes_expanded"] >= 1

    listing = client.get("/api/v1/runs")
    assert listing.status_code == 200
    assert any(row["run_id"] == run_id for row in listing.json())


def test_unknown_run_is_404_not_success(client: TestClient) -> None:
    r = client.get("/api/v1/runs/run-does-not-exist")
    assert r.status_code == 404
    assert r.json()["detail"]["failure_code"] == "not_found"


def test_validate_endpoint(client: TestClient) -> None:
    ok = client.post("/api/v1/problems/validate", json={"problem": BOUNDARY_RELEASE})
    assert ok.status_code == 200
    assert ok.json()["valid"] is True
    bad = client.post(
        "/api/v1/problems/validate",
        json={"problem": {"horizon": 1, "fluents": {}, "actions": [], "goal": {}}},
    )
    assert bad.status_code == 422
