"""Evidence-store and service-orchestration tests."""

from __future__ import annotations

import json

from tplan.model import FailureCode
from tplan.store import EvidenceStore

from .cases import BOUNDARY_RELEASE, CONSUMABLE, UNSAT


def test_successful_run_persists_problem_schedule_timeline_and_trace(
    service, settings
) -> None:
    resp = service.solve(BOUNDARY_RELEASE)
    assert resp.status == "optimal"
    assert resp.run_id and resp.input_sha256

    store = EvidenceStore(settings.db_path)
    try:
        record = store.get_run(resp.run_id)
    finally:
        store.close()
    assert record is not None
    # Exact input is stored and fingerprint-stable.
    assert record["problem"]["horizon"] == BOUNDARY_RELEASE["horizon"]
    assert len(record["problem_sha256"]) == 64
    # Concrete schedule and full timeline.
    assert [(s["action_id"], s["start"], s["end"]) for s in record["schedule"]] == [
        ("A", 0, 3),
        ("B", 3, 5),
    ]
    kinds = {e["kind"] for e in record["timeline"]}
    assert {"init", "start", "end"} <= kinds
    # The boundary hand-off order is present in the stored evidence.
    at_3 = [e for e in record["timeline"] if e["time"] == 3]
    assert at_3[0]["kind"] == "end" and at_3[1]["kind"] == "start"
    # Search trace carries decision steps.
    assert any(e.get("event") == "plan_found" for e in record["trace"])


def test_unsat_run_is_persisted_with_explicit_failure_code(service, settings) -> None:
    resp = service.solve(UNSAT)
    assert resp.status == "unsat"
    assert resp.failure_code == FailureCode.UNSAT_PROVEN.value
    store = EvidenceStore(settings.db_path)
    try:
        record = store.get_run(resp.run_id)
    finally:
        store.close()
    assert record["status"] == "unsat"
    assert record["failure_code"] == "unsat_proven"
    assert record["schedule"] == []


def test_invalid_input_is_recorded_not_returned_as_success(service, settings) -> None:
    bad = {"horizon": -1, "fluents": {}, "actions": [], "goal": {}}
    resp = service.solve(bad)
    assert resp.status == "invalid"
    assert resp.failure_code == FailureCode.INVALID_PROBLEM.value
    assert "horizon" in resp.message
    store = EvidenceStore(settings.db_path)
    try:
        record = store.get_run(resp.run_id)
    finally:
        store.close()
    assert record["status"] == "invalid"
    assert record["failure_code"] == "invalid_problem"


def test_replay_endpoint_reports_specific_category(service) -> None:
    # Overlapping capacity-1 users -> resource conflict at the second start.
    result = service.replay(
        BOUNDARY_RELEASE,
        [
            {"action_id": "A", "start": 0, "end": 3},
            {"action_id": "B", "start": 2, "end": 4},
        ],
    )
    assert result["ok"] is False
    assert result["failure_time"] == 2
    assert result["failure_code"] in {
        FailureCode.RESOURCE_CONFLICT.value,
        FailureCode.START_CONDITION_VIOLATED.value,
    }
    # Timeline up to the failure is still returned as evidence.
    assert result["timeline"][0]["kind"] == "init"


def test_replay_accepts_valid_schedule_and_confirms_goal(service) -> None:
    result = service.replay(
        BOUNDARY_RELEASE,
        [
            {"action_id": "A", "start": 0, "end": 3},
            {"action_id": "B", "start": 3, "end": 5},
        ],
    )
    assert result["ok"] is True
    assert result["goal_met"] is True
    assert result["failure_code"] is None


def test_consumable_service_path(service) -> None:
    resp = service.solve(CONSUMABLE)
    assert resp.status == "optimal"
    # Two burns of 2 from stock 3 cannot overlap; plan still reaches goal.
    assert resp.best_cost == 1


def test_list_runs_returns_most_recent_first(service, settings) -> None:
    ids = [service.solve(UNSAT).run_id for _ in range(3)]
    store = EvidenceStore(settings.db_path)
    try:
        listing = store.list_runs()
    finally:
        store.close()
    listed_ids = [r["run_id"] for r in listing[:3]]
    assert listed_ids == list(reversed(ids))


def test_engine_versions_attached_to_response(service) -> None:
    resp = service.solve(UNSAT)
    assert resp.engine["service_version"]
    assert resp.engine["python"].count(".") == 2
    assert resp.elapsed_seconds >= 0
    assert resp.nodes_expanded > 0
    assert json.dumps(resp.trace, default=str) is not None  # trace is JSON-able
