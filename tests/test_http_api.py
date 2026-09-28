"""End-to-end HTTP tests using FastAPI's in-process test client."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from strips_planner.service import create_app
from tests.conftest import load


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "http-evidence.db"))
    with TestClient(app) as test_client:
        yield test_client


def _request(domain_file, problem_file, options=None):
    payload = {"domain": load(domain_file), "problem": load(problem_file)}
    if options:
        payload["options"] = options
    return payload


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_plan_solved_envelope_and_run_lookup(client):
    response = client.post(
        "/plan",
        json=_request("domain_route_graph.json", "problem_route_cost.json",
                      {"algorithm": "ucs", "heuristic": "zero"}),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "solved"
    assert body["cost"] == 3
    assert body["verification"]["valid"] is True
    run_id = body["run_id"]

    fetched = client.get(f"/runs/{run_id}").json()
    assert fetched["result_status"] == "solved"

    steps = client.get(f"/runs/{run_id}/steps").json()["steps"]
    assert [s["action"] for s in steps] == [
        "walk(a, b)", "walk(b, c)", "walk(c, d)"
    ]

    trace = client.get(f"/runs/{run_id}/trace").json()["trace"]
    assert trace[0]["g"] == 0


def test_unsolvable_is_200_with_verdict(client):
    response = client.post(
        "/plan",
        json=_request("domain_resource_ops.json",
                      "problem_unsolvable_sealed.json",
                      {"time_limit_seconds": 120.0}),
    )
    assert response.status_code == 200
    assert response.json()["verdict"] == "unsolvable"


def test_unknown_is_200_with_bound_reason(client):
    response = client.post(
        "/plan",
        json=_request(
            "domain_resource_ops.json",
            "problem_unsolvable_missing.json",
            {"max_expansions": 2},
        ),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "unknown"
    assert body["reason"] == "node_limit"
    assert body["plan"] == []


def test_input_error_is_422_with_code(client):
    payload = _request("domain_resource_ops.json", "problem_solvable.json")
    del payload["domain"]["actions"][0]["parameters"]
    response = client.post("/plan", json=payload)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["category"] == "input_error"
    assert error["code"] == "PARAM_MISSING"
    assert "actions[0]" in error["message"]


def test_malformed_json_is_422_request_malformed(client):
    response = client.post("/plan", content=b"{not json",
                           headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_MALFORMED"


def test_state_conflict_surface_via_replay_endpoint(client):
    # Store a run, then replay it through the HTTP route.
    first = client.post(
        "/plan",
        json=_request("domain_route_graph.json", "problem_route_cost.json"),
    ).json()
    replay = client.post(f"/runs/{first['run_id']}/replay")
    assert replay.status_code == 200
    assert replay.json()["replayed_from"] == first["run_id"]
    assert replay.json()["cost"] == first["cost"]


def test_missing_run_is_404_run_not_found(client):
    response = client.get("/runs/run-does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RUN_NOT_FOUND"


def test_resource_exhausted_is_507(client):
    payload = _request(
        "domain_route_graph.json", "problem_route_cost.json",
        {"ground_actions_limit": 1},
    )
    response = client.post("/plan", json=payload)
    assert response.status_code == 507
    assert response.json()["error"]["category"] == "resource_exhausted"


def test_runs_listing_contains_recorded_run(client):
    client.post(
        "/plan",
        json=_request("domain_route_graph.json", "problem_route_cost.json"),
    )
    listing = client.get("/runs").json()["runs"]
    assert len(listing) == 1
    assert listing[0]["domain_name"] == "route-graph"
