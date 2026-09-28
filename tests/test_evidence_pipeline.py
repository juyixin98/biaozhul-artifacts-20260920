"""Evidence persistence, error distinction and request replay."""

from __future__ import annotations

import pytest

from strips_planner.errors import (
    GROUNDING_LIMIT,
    ValidationError,
)
from strips_planner.pipeline import run_pipeline
from tests.conftest import load


def test_successful_run_persists_run_steps_and_trace(store, plan_request):
    request = plan_request(
        "domain_resource_ops.json", "problem_solvable.json",
        {"algorithm": "astar", "heuristic": "h_max"},
    )
    response = run_pipeline(request, store)
    run_id = response["run_id"]

    record = store.get_run(run_id)
    assert record["status"] == "ok"
    assert record["result_status"] == "solved"
    assert record["cost"] == 8

    steps = store.get_steps(run_id)
    assert len(steps) == 6
    assert steps[0]["action"] == "move(w1, bench, press)"
    # State encodings round-trip through SQLite.
    assert "at(w1, press)" in steps[0]["state_after"]
    assert steps[-1]["state_after"]  # final state non-empty evidence

    trace = store.get_trace(run_id)
    assert trace[0]["g"] == 0
    assert any(t["depth"] >= 2 for t in trace)


def test_unsolvable_run_persists_verdict_without_plan(store, plan_request):
    request = plan_request(
        "domain_resource_ops.json", "problem_unsolvable_missing.json",
        # Exhaustion must complete even under coverage instrumentation.
        {"algorithm": "astar", "heuristic": "h_max",
         "time_limit_seconds": 120.0},
    )
    response = run_pipeline(request, store)
    assert response["verdict"] == "unsolvable"
    record = store.get_run(response["run_id"])
    assert record["result_status"] == "unsolvable"
    assert record["plan_json"] is None


def test_unknown_run_persists_bound_reason(store, plan_request):
    request = plan_request(
        "domain_resource_ops.json", "problem_unsolvable_missing.json",
        {"algorithm": "astar", "heuristic": "h_max", "max_expansions": 3},
    )
    response = run_pipeline(request, store)
    assert response["verdict"] == "unknown"
    assert response["reason"] == "node_limit"
    record = store.get_run(response["run_id"])
    assert record["reason"] == "node_limit"


def test_input_error_is_persisted_with_distinct_category(store, plan_request):
    request = plan_request(
        "domain_resource_ops.json", "problem_solvable.json"
    )
    del request["domain"]["actions"][0]["name"]
    with pytest.raises(ValidationError) as exc:
        run_pipeline(request, store)
    assert exc.value.category == "input_error"

    runs = store.list_runs()
    assert runs[0]["status"] == "input_error"
    record = store.get_run(runs[0]["run_id"])
    assert record["error"]["category"] == "input_error"
    assert record["error"]["code"] == "ACTION_NAME_MISSING"


def test_resource_exhausted_distinguishable_from_input_error(store, plan_request):
    request = plan_request(
        "domain_route_graph.json", "problem_route_cost.json",
        {"ground_actions_limit": 1},
    )
    from strips_planner.errors import ResourceLimitError

    with pytest.raises(ResourceLimitError) as exc:
        run_pipeline(request, store)
    assert exc.value.category == "resource_exhausted"
    assert exc.value.code == GROUNDING_LIMIT
    record = store.get_run(_latest_run_id(store))
    assert record["status"] == "resource_exhausted"
    assert record["error"]["code"] == GROUNDING_LIMIT


def test_replay_reproduces_same_verdict(store, plan_request):
    request = plan_request(
        "domain_resource_ops.json", "problem_cost_paths.json",
        {"algorithm": "ucs", "heuristic": "zero"},
    )
    first = run_pipeline(request, store)
    replayed_payload = store.replay_request(first["run_id"])
    second = run_pipeline(replayed_payload, store)
    assert second["verdict"] == first["verdict"] == "solved"
    assert second["cost"] == first["cost"] == 1
    assert second["run_id"] != first["run_id"]


def test_run_ids_are_unique_and_sortable(store, plan_request):
    request = plan_request(
        "domain_route_graph.json", "problem_route_cost.json"
    )
    ids = {run_pipeline(request, store)["run_id"] for _ in range(3)}
    assert len(ids) == 3
    assert all(rid.startswith("run-") for rid in ids)


def _latest_run_id(store):
    return store.list_runs()[0]["run_id"]
