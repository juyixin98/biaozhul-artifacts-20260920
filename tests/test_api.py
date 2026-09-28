"""End-to-end HTTP tests over the real FastAPI app and SQLite store."""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from strips_planner.api.app import create_app
from strips_planner.errors import ErrorCategory
from tests.conftest import load_fixture

pytestmark = pytest.mark.integration


@pytest.fixture
def client(tmp_path) -> TestClient:
    app = create_app(
        db_path=str(tmp_path / "evidence.db"),
        log_path=str(tmp_path / "runs.jsonl"),
    )
    with TestClient(app) as test_client:
        yield test_client


def test_healthz_ok(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_plan_success_is_verified_and_persisted(client: TestClient) -> None:
    payload = load_fixture("resource_ops.json")
    response = client.post("/api/v1/plans", json={"problem": payload})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    result = body["result"]
    run_id = body["run_id"]
    assert result["status"] == "FOUND"
    # Cheapest ground truth: cost 9 over 5 actions (clear+pick+2 moves+drop).
    assert result["path_cost"] == 9.0
    assert result["plan_length"] == 5
    assert result["optimal"] is True
    assert result["execution"]["valid"] is True
    assert result["execution"]["goal_reached"] is True
    assert result["execution"]["total_cost"] == 9.0
    # Every step carries predecessor and successor state evidence.
    steps = result["execution"]["steps"]
    assert len(steps) == 5
    assert all(step["status"] == "OK" for step in steps)
    assert any(step["action"] == "(clear_block site1)"
               and "(blocked site1)" in step["state_before"]
               and "(blocked site1)" not in step["state_after"]
               for step in steps)

    # The same run is fully retrievable by id with its replay timeline.
    fetched = client.get(f"/api/v1/runs/{run_id}").json()["result"]
    assert fetched["status"] == "FOUND"
    assert fetched["path_cost"] == 9.0
    stages = [event["stage"] for event in fetched["replay_log"]]
    assert stages == ["RECEIVED", "PARSED", "VALIDATED", "SEARCH", "EXECUTED", "COMPLETED"]
    search_event = next(e for e in fetched["replay_log"] if e["stage"] == "SEARCH")
    assert search_event["status"] == "FOUND"
    assert search_event["expanded"] >= 1


def test_plan_unsolvable_goal_reports_unsolvable(client: TestClient) -> None:
    payload = load_fixture("resource_ops_unsolvable.json")
    response = client.post("/api/v1/plans", json={"problem": payload})
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["result"]["status"] == "UNSOLVABLE"
    assert body["result"]["plan"] == []
    assert "unreachable" in body["result"]["search"]["reason"]


def test_plan_resource_limit_is_distinct_unknown_category(client: TestClient) -> None:
    payload = load_fixture("resource_ops.json")
    response = client.post("/api/v1/plans", json={
        "problem": payload,
        "options": {"algorithm": "astar", "heuristic": "hmax", "max_depth": 1},
    })
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    error = body["error"]
    assert error["category"] == ErrorCategory.RESOURCE_LIMIT
    assert error["details"]["bound"] == "max_depth"
    assert error["details"]["solvability"] == "UNKNOWN"
    assert body["result"]["status"] == "LIMIT"


def test_invalid_problem_returns_422_with_issue_list(client: TestClient) -> None:
    payload = load_fixture("resource_ops.json")
    payload["init"].append("(at r1 mars)")  # undeclared object
    response = client.post("/api/v1/plans", json={"problem": payload})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["category"] == ErrorCategory.INVALID_PROBLEM
    codes = {issue["code"] for issue in error["details"]["issues"]}
    assert "UNKNOWN_REFERENCE" in codes


def test_malformed_json_is_input_invalid_and_persisted(client: TestClient) -> None:
    response = client.post(
        "/api/v1/plans", content="{not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    body = response.json()
    error = body["error"]
    assert error["category"] == ErrorCategory.INPUT_INVALID
    assert error["code"] == "SYNTAX_ERROR"
    # Even undecodable input gets a replayable run id and an evidence row.
    run_id = body["run_id"]
    assert run_id
    stored = client.get(f"/api/v1/runs/{run_id}").json()["result"]
    assert stored["category"] == ErrorCategory.INPUT_INVALID
    assert stored["status"] == "REJECTED"
    assert stored["request"]["raw_body_preview"] == "{not json"


def test_empty_body_is_rejected_with_run_id(client: TestClient) -> None:
    response = client.post("/api/v1/plans", content="",
                           headers={"content-type": "application/json"})
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "MISSING_FIELD"
    assert client.get(f"/api/v1/runs/{body['run_id']}").status_code == 200


def test_add_delete_conflict_is_422(client: TestClient) -> None:
    payload = {
        "name": "conflict",
        "types": ["loc"],
        "objects": {"loc": ["a"]},
        "predicates": {"p": {"types": ["loc"], "static": False}},
        "actions": [{
            "name": "bad", "cost": 1,
            "parameters": [{"name": "?x", "type": "loc"}],
            "preconditions": {"pos": [], "neg": []},
            "effects": {"add": ["(p ?x)"], "del": ["(p ?x)"]},
        }],
        "init": [],
        "goal": {"pos": ["(p a)"], "neg": []},
    }
    response = client.post("/api/v1/plans", json={"problem": payload})
    assert response.status_code == 422
    codes = {i["code"] for i in response.json()["error"]["details"]["issues"]}
    assert "ADD_DELETE_CONFLICT" in codes


def test_execute_endpoint_detects_state_conflict(client: TestClient) -> None:
    payload = load_fixture("resource_ops.json")
    response = client.post("/api/v1/plans/execute", json={
        "problem": payload,
        "plan": ["(move r1 depot site1)"],  # site1 blocked: negative precondition
    })
    assert response.status_code == 409
    body = response.json()
    assert body["success"] is False
    error = body["error"]
    assert error["category"] == ErrorCategory.STATE_CONFLICT
    assert error["code"] == "NEGATIVE_PRECONDITION_VIOLATED"
    assert error["details"]["failure_step"] == 0
    assert any("(blocked site1)" in lit
               for lit in error["details"]["violated_literals"])


def test_execute_endpoint_accepts_independently_valid_plan(client: TestClient) -> None:
    payload = load_fixture("resource_ops.json")
    plan = [
        "(clear_block site1)",
        "(pick r1 crate_a depot)",
        "(move r1 depot site1)",
        "(move r1 site1 site2)",
        "(drop r1 crate_a site2)",
    ]
    response = client.post("/api/v1/plans/execute", json={
        "problem": payload, "plan": plan,
    })
    assert response.status_code == 200, response.text
    execution = response.json()["result"]["execution"]
    assert execution["valid"] is True
    assert execution["total_cost"] == 9.0


def test_execute_unknown_action_is_state_conflict_category(client: TestClient) -> None:
    payload = load_fixture("resource_ops.json")
    response = client.post("/api/v1/plans/execute", json={
        "problem": payload, "plan": ["(warp r1 site2)"],
    })
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "UNKNOWN_ACTION"


def test_unknown_run_id_is_404(client: TestClient) -> None:
    response = client.get("/api/v1/runs/run-does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["category"] == ErrorCategory.NOT_FOUND


def test_runs_listing_and_filtering(client: TestClient) -> None:
    ok_payload = load_fixture("resource_ops.json")
    bad_payload = load_fixture("resource_ops_unsolvable.json")
    client.post("/api/v1/plans", json={"problem": ok_payload})
    client.post("/api/v1/plans", json={"problem": bad_payload})

    listing = client.get("/api/v1/runs").json()["result"]
    assert listing["count"] >= 2
    unsolvable = client.get("/api/v1/runs", params={"status": "UNSOLVABLE"}).json()["result"]
    assert len(unsolvable["runs"]) == 1
    assert unsolvable["runs"][0]["status"] == "UNSOLVABLE"


def test_reference_prior_run_problem_for_execution(client: TestClient) -> None:
    payload = load_fixture("resource_ops.json")
    planned = client.post("/api/v1/plans", json={"problem": payload}).json()
    run_id = planned["run_id"]
    plan = planned["result"]["plan"]
    # Replay the planner's plan referencing only the stored run id.
    replay = client.post("/api/v1/plans/execute", json={
        "run_id": run_id, "plan": plan,
    })
    assert replay.status_code == 200
    assert replay.json()["result"]["execution"]["valid"] is True


def test_bad_options_are_input_invalid(client: TestClient) -> None:
    payload = load_fixture("resource_ops.json")
    response = client.post("/api/v1/plans", json={
        "problem": payload,
        "options": {"max_expanded": -3},
    })
    assert response.status_code == 400
    assert response.json()["error"]["category"] == ErrorCategory.INPUT_INVALID


def test_non_admissible_heuristic_is_computation_failure(client: TestClient) -> None:
    payload = load_fixture("resource_ops.json")
    response = client.post("/api/v1/plans", json={
        "problem": payload,
        "options": {"algorithm": "astar", "heuristic": "goalcount"},
    })
    assert response.status_code == 500
    error = response.json()["error"]
    assert error["category"] == ErrorCategory.COMPUTATION_FAILED
    assert error["code"] == "NON_ADMISSIBLE_HEURISTIC"


def test_expanded_bound_hits_resource_limit_with_counters(client: TestClient) -> None:
    payload = load_fixture("resource_ops.json")
    response = client.post("/api/v1/plans", json={
        "problem": payload,
        "options": {"algorithm": "bfs", "max_expanded": 1},
    })
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["status"] == "LIMIT"
    assert body["error"]["category"] == ErrorCategory.RESOURCE_LIMIT
    assert body["error"]["details"]["expanded"] >= 1
    assert body["error"]["details"]["bound_value"] == 1.0


def test_execute_requires_plan_list(client: TestClient) -> None:
    payload = load_fixture("resource_ops.json")
    response = client.post("/api/v1/plans/execute", json={
        "problem": payload, "plan": "(clear_block site1)",
    })
    assert response.status_code == 400
    assert response.json()["error"]["category"] == ErrorCategory.INPUT_INVALID


def test_execute_requires_problem_or_run_reference(client: TestClient) -> None:
    response = client.post("/api/v1/plans/execute", json={"plan": ["(wait r1)"]})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "MISSING_FIELD"


def test_each_error_category_is_distinguishable(client: TestClient) -> None:
    payload = load_fixture("resource_ops.json")

    cases = [
        ({"problem": payload, "options": {"max_depth": 1}}, 200,
         ErrorCategory.RESOURCE_LIMIT),
        ({"problem": {**payload, "init": ["(nope)"]}}, 422,
         ErrorCategory.INVALID_PROBLEM),
        ("not-json", 400, ErrorCategory.INPUT_INVALID),
    ]
    for body, expected_status, expected_category in cases:
        if isinstance(body, str):
            response = client.post(
                "/api/v1/plans", content=body,
                headers={"content-type": "application/json"},
            )
        else:
            response = client.post("/api/v1/plans", json=body)
        assert response.status_code == expected_status, (body, response.text)
        assert response.json()["error"]["category"] == expected_category


def test_application_entrypoint_builds_app(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "evidence.db"
    log_path = tmp_path / "runs.jsonl"
    monkeypatch.setenv("STRIPS_DB_PATH", str(db_path))
    monkeypatch.setenv("STRIPS_LOG_PATH", str(log_path))
    import importlib

    import strips_planner.main as main_module

    importlib.reload(main_module)
    with TestClient(main_module.app) as test_client:
        assert test_client.get("/healthz").status_code == 200
