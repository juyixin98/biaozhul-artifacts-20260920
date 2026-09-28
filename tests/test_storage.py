"""Tests for the SQLite evidence store and JSONL run logger."""

from __future__ import annotations

import json

import pytest

from strips_planner.errors import ErrorCategory, RunNotFoundError
from strips_planner.storage.database import EvidenceStore, RunRecord
from strips_planner.storage.run_log import RunLogger

pytestmark = pytest.mark.integration


def _record(run_id: str = "run-1", **overrides) -> RunRecord:
    data = dict(
        run_id=run_id,
        created_at="2026-09-28T00:00:00.000000+00:00",
        category="OK",
        success=True,
        status="FOUND",
        request={"name": "demo"},
        problem_name="demo",
        algorithm="astar",
        heuristic="hmax",
        optimal=True,
        path_cost=9.0,
        plan=["(pick r1 crate_a depot)"],
        problem_summary={"ground_action_count": 12},
        search={"expanded": 4},
        validation=None,
        error=None,
        stats={"execution_steps": 1},
    )
    data.update(overrides)
    return RunRecord(**data)


def test_run_round_trips_with_full_evidence(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "evidence.db")
    store.save_run(_record())
    row = store.get_run("run-1")
    assert row["status"] == "FOUND"
    assert row["success"] is True
    assert row["optimal"] is True
    assert row["plan"] == ["(pick r1 crate_a depot)"]
    assert row["problem"]["ground_action_count"] == 12
    assert row["search"]["expanded"] == 4
    store.close()


def test_unknown_run_is_distinct_not_found_error(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "evidence.db")
    with pytest.raises(RunNotFoundError) as exc:
        store.get_run("run-missing")
    assert exc.value.category == ErrorCategory.NOT_FOUND
    assert exc.value.details["run_id"] == "run-missing"


def test_list_runs_filters_by_status_and_category(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "evidence.db")
    store.save_run(_record("run-a", status="FOUND", category="OK"))
    store.save_run(_record("run-b", status="LIMIT", category=ErrorCategory.RESOURCE_LIMIT,
                           success=False, optimal=None, path_cost=None, plan=None))
    store.save_run(_record("run-c", status="UNSOLVABLE", category="OK"))

    limited = store.list_runs(status="LIMIT")
    assert [r["run_id"] for r in limited] == ["run-b"]
    ok = store.list_runs(category="OK")
    assert {r["run_id"] for r in ok} == {"run-a", "run-c"}


def test_duplicate_run_id_is_storage_error(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "evidence.db")
    store.save_run(_record())
    from strips_planner.errors import StorageError
    with pytest.raises(StorageError):
        store.save_run(_record())


def test_jsonl_log_records_replayable_run_timeline(tmp_path) -> None:
    log_path = tmp_path / "runs.jsonl"
    logger = RunLogger(log_path)
    logger.event("run-x", "RECEIVED", reason="planning request received")
    logger.event("run-x", "SEARCH", status="FOUND", expanded=7, reason="goal popped")
    logger.event("run-y", "RECEIVED")

    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert [line["run_id"] for line in lines] == ["run-x", "run-x", "run-y"]
    assert lines[1]["stage"] == "SEARCH"
    assert lines[1]["expanded"] == 7

    replay = logger.read("run-x")
    assert [event["stage"] for event in replay] == ["RECEIVED", "SEARCH"]
    assert all("ts" in event and "reason" in event for event in replay)


def test_jsonl_log_coerces_non_finite_numbers(tmp_path) -> None:
    logger = RunLogger(tmp_path / "runs.jsonl")
    logger.event("run-z", "SEARCH", cost=float("inf"))
    raw = (tmp_path / "runs.jsonl").read_text()
    assert json.loads(raw)["cost"] == "Infinity"
