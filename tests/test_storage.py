"""Concrete tests for the SQLite evidence store."""
from __future__ import annotations

import json

import pytest

from app.planner.replay import replay
from app.planner.solver import SolverConfig, solve
from app.rules.models import Plan, ScheduledAction
from app.storage import canonical_json


@pytest.mark.storage
def test_search_run_persists_status_budget_and_honest_optimality(store, workshop_problem) -> None:
    result = solve(workshop_problem, SolverConfig(budget_nodes=800, max_steps=8))
    run_id = store.save_search(workshop_problem, result)

    record = store.get_run(run_id)
    assert record is not None
    assert record["run_id"] == run_id
    assert record["run_id"].startswith("solve-")
    assert record["status"] == "FEASIBLE_UNPROVEN"
    assert record["optimal"] == 0
    assert record["makespan"] == 4
    assert record["nodes_expanded"] > 800
    assert record["budget_nodes"] == 800
    assert "bound 3" in record["reason"]
    # Fingerprint is deterministic for identical input.
    assert len(record["input_fingerprint"]) == 64
    again = store.save_search(workshop_problem, result)
    assert store.get_run(again)["input_fingerprint"] == record["input_fingerprint"]
    assert again != run_id  # identity differs, input provenance identical


@pytest.mark.storage
def test_replay_run_persists_full_timeline_and_typed_violations(store, reactor_problem) -> None:
    plan = Plan(steps=[
        ScheduledAction(action="run_pump", start=0, duration=3),
        ScheduledAction(action="vent", start=1, duration=0),
    ])
    verdict = replay(reactor_problem, plan)
    run_id = store.save_replay(reactor_problem, plan, verdict)

    events = store.get_events(run_id)
    assert events  # non-empty timeline
    zero = [e for e in events if e["kind"] == "ZERO_DURATION" and e["action"] == "vent"]
    assert len(zero) == 1
    assert json.loads(zero[0]["state_before_json"])["coolant"] == 2
    assert json.loads(zero[0]["state_after_json"])["coolant"] == 0

    violations = store.get_violations(run_id)
    cats = {(v["category"], v["time"]) for v in violations}
    assert ("INVARIANT_VIOLATION", 1) in cats
    assert record_outcome_invalid(store, run_id)


def record_outcome_invalid(store, run_id: str) -> bool:
    return store.get_run(run_id)["outcome"] == "INVALID"


@pytest.mark.storage
def test_reference_check_records_agreement(store, drone_problem) -> None:
    result = solve(drone_problem, SolverConfig(budget_nodes=50_000, max_steps=6))
    run_id = store.save_search(drone_problem, result)
    store.save_reference_check(
        run_id,
        reference_kind="small_grid_exhaustive",
        reference_found=True,
        reference_makespan=4,
        solver_found=True,
        solver_makespan=4,
        agree=True,
        schedules_evaluated=123,
        detail="match",
    )
    check = store.get_reference_check(run_id)
    assert check["agree"] == 1
    assert check["reference_makespan"] == check["solver_makespan"] == 4


@pytest.mark.storage
def test_list_and_filter_runs(store, reactor_problem) -> None:
    result = solve(reactor_problem)
    store.save_search(reactor_problem, result)
    verdict = replay(reactor_problem, Plan(steps=[]))
    store.save_replay(reactor_problem, Plan(steps=[]), verdict)

    kinds = {row["kind"] for row in store.list_runs()}
    assert {"solve", "replay"} <= kinds
    assert all(row["kind"] == "solve" for row in store.list_runs(kind="solve"))


@pytest.mark.storage
def test_canonical_json_is_key_sorted(store) -> None:
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


@pytest.mark.storage
def test_unknown_run_and_events_are_absent(store) -> None:
    assert store.get_run("does-not-exist") is None
    assert store.get_events("does-not-exist") == []
    assert store.get_violations("does-not-exist") == []
