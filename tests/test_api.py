"""Concrete HTTP interface tests via FastAPI TestClient."""
from __future__ import annotations

import pytest

from app.rules.loader import load_file


def payload(problem, **options) -> dict:
    body = {"problem": problem.model_dump(mode="json")}
    if options:
        body["options"] = options
    return body


@pytest.mark.api
def test_health_and_version_report_versions(client) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    version = client.get("/version")
    assert version.status_code == 200
    assert version.json()["service"]
    assert version.json()["python"].startswith("3.")


@pytest.mark.api
def test_solve_returns_optimal_plan_and_persisted_run_identity(client, drone_problem) -> None:
    response = client.post("/api/solve", json=payload(drone_problem, budget_nodes=100_000, max_steps=8))
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["search"]["status"] == "OPTIMAL"
    assert body["search"]["optimal"] is True
    assert body["search"]["makespan"] == 4
    assert body["replay"]["outcome"] == "VALID"
    run_id = body["run"]["run_id"]
    assert len(body["run"]["input_fingerprint"]) == 64
    assert body["run"]["engine_version"]

    # Evidence is retrievable by run identity.
    fetched = client.get(f"/api/runs/{run_id}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "OPTIMAL"


@pytest.mark.api
def test_solve_budget_expiry_is_reported_not_hidden(client, workshop_problem) -> None:
    response = client.post("/api/solve", json=payload(workshop_problem, budget_nodes=800, max_steps=8))
    assert response.status_code == 200
    body = response.json()
    assert body["search"]["status"] == "FEASIBLE_UNPROVEN"
    assert body["search"]["optimal"] is False
    assert body["search"]["plan"] is not None
    assert body["replay"]["outcome"] == "VALID"
    assert "bound 3" in body["search"]["reason"]


@pytest.mark.api
def test_replay_invalid_plan_returns_typed_violation_but_http_200(client, reactor_problem) -> None:
    plan = {"steps": [
        {"action": "run_pump", "start": 0, "duration": 3},
        {"action": "vent", "start": 1, "duration": 0},
    ]}
    response = client.post("/api/replay", json={
        "problem": reactor_problem.model_dump(mode="json"),
        "plan": plan,
    })
    assert response.status_code == 200
    body = response.json()
    assert body["replay"]["outcome"] == "INVALID"
    categories = {v["category"] for v in body["replay"]["violations"]}
    assert "INVARIANT_VIOLATION" in categories

    run_id = body["run"]["run_id"]
    events = client.get(f"/api/runs/{run_id}/events").json()["events"]
    assert any(e["kind"] == "ZERO_DURATION" and e["action"] == "vent" for e in events)


@pytest.mark.api
def test_invalid_problem_is_400_with_category_not_a_200_success(client) -> None:
    bad = {
        "name": "broken",
        "horizon": 2,
        "goal": {"fact": {"fact": "x", "op": "==", "value": 1}},
        "actions": [
            {"name": "a", "duration_min": 5, "effects": []}  # exceeds horizon
        ],
    }
    response = client.post("/api/solve", json={"problem": bad})
    assert response.status_code == 400
    assert response.json()["error"]["category"] == "INVALID_INPUT"


@pytest.mark.api
def test_malformed_json_is_400_typed_error_not_500(client) -> None:
    response = client.post(
        "/api/solve",
        data="{not json",
        headers={"content-type": "application/json"},
    )
    # A request-shape defect is a client error carrying a typed category;
    # it must never surface as a 500 or as a successful solve.
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["category"] == "INVALID_INPUT"


@pytest.mark.api
def test_unknown_run_is_404_with_not_found_envelope(client) -> None:
    response = client.get("/api/runs/nope")
    assert response.status_code == 404
    assert response.json()["error"]["category"] == "NOT_FOUND"


@pytest.mark.api
def test_unknown_run_subresources_are_404_envelopes_too(client) -> None:
    events = client.get("/api/runs/nope/events")
    violations = client.get("/api/runs/nope/violations")
    assert events.status_code == violations.status_code == 404
    assert events.json()["error"]["category"] == "NOT_FOUND"
    assert violations.json()["error"]["category"] == "NOT_FOUND"


@pytest.mark.api
def test_reference_endpoint_matches_solver_makespan(client, drone_problem) -> None:
    response = client.post("/api/reference", json={
        "problem": drone_problem.model_dump(mode="json"),
        "max_steps": 5,
        "max_occurrences_per_action": 4,
    })
    assert response.status_code == 200
    body = response.json()
    assert body["found"] is True
    assert body["optimal_makespan"] == 4
    assert body["schedules_evaluated"] > 0


@pytest.mark.api
def test_cross_check_reference_agreement_is_persisted(client, drone_problem) -> None:
    response = client.post("/api/solve", json=payload(
        drone_problem,
        budget_nodes=100_000,
        max_steps=5,
        cross_check_reference=True,
        reference_max_steps=5,
        reference_max_occurrences=4,
    ))
    assert response.status_code == 200
    body = response.json()
    assert body["cross_check_agrees"] is True
    assert body["reference"]["optimal_makespan"] == body["search"]["makespan"] == 4


@pytest.mark.api
def test_runs_listing_includes_searches(client, workshop_problem) -> None:
    client.post("/api/solve", json=payload(workshop_problem, budget_nodes=100_000, max_steps=8))
    listing = client.get("/api/runs?kind=solve")
    assert listing.status_code == 200
    rows = listing.json()["runs"]
    assert rows and all(row["kind"] == "solve" for row in rows)
