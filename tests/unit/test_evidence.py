"""Tests for run identity, structured logging and SQLite persistence."""
from __future__ import annotations

import json

import pytest

from sample_size_planner.contracts import FailureCategory
from sample_size_planner.evidence.repository import RunRepository
from sample_size_planner.evidence.run_log import RunIdentity, RunLogger, dependency_versions


@pytest.mark.unit
def test_run_identity_carries_versions_and_correlation_id() -> None:
    run = RunIdentity.new("unit-probe")
    assert run.run_id.startswith("run-")
    versions = dependency_versions()
    assert {"python", "numpy", "scipy", "fastapi", "pydantic"} <= versions.keys()
    assert run.versions["scipy"] == versions["scipy"]


@pytest.mark.unit
def test_log_lines_are_correlated_and_record_judgement(tmp_path) -> None:
    run = RunIdentity.new("judgement-probe")
    path = tmp_path / f"{run.run_id}.jsonl"
    log = RunLogger(run, path, echo=False)
    log.input_recorded("scenario", {"alpha": 0.05, "power": 0.8, "effect": 0.8})
    log.progress("integer search", n=25)
    log.judgement("accept", "boundary n passes and n-1 fails", n=25, power=0.807)

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert all(row["run_id"] == run.run_id for row in rows)
    steps = [row["step"] for row in rows]
    assert steps == ["input", "progress", "judgement"]
    verdict_row = next(r for r in rows if r["step"] == "judgement")
    assert verdict_row["verdict"] == "accept"
    assert verdict_row["n"] == 25


@pytest.mark.unit
def test_failure_judgement_is_distinct_from_success(tmp_path) -> None:
    run = RunIdentity.new("failure-probe")
    path = tmp_path / f"{run.run_id}.jsonl"
    log = RunLogger(run, path, echo=False)
    log.judgement("failure", "effect is zero",
                  failure_category=FailureCategory.EFFECT_ZERO.value)
    row = json.loads(path.read_text().splitlines()[0])
    assert row["verdict"] == "failure"
    assert row["failure_category"] == "effect_zero"
    assert row["verdict"] != "accept"


@pytest.mark.unit
def test_unknown_verdict_rejected(tmp_path) -> None:
    run = RunIdentity.new("bad-verdict")
    log = RunLogger(run, tmp_path / f"{run.run_id}.jsonl", echo=False)
    with pytest.raises(ValueError):
        log.judgement("maybe", "uncertain")  # unknown state cannot be logged


@pytest.mark.integration
def test_repository_roundtrip_and_run_correlation(tmp_path) -> None:
    repo = RunRepository(tmp_path / "db" / "runs.db")
    run = RunIdentity.new("persist-probe")
    repo.start_run(run.run_id, run.created_at, "plan-normal", run.versions)

    result = {
        "success": True, "failure_category": "none", "n_per_group0": 25,
        "n_per_group1": 25, "n_total": 50, "achieved_power": 0.807,
        "method": "normal_approx",
    }
    plan_id = repo.save_plan(run.run_id, "normal", {"effect": 0.8}, result)
    sim_id = repo.save_simulation(run.run_id, "normal", {"effect": 0.8},
                                  50_000, 0.806, 0.802, 0.810, True)
    assert isinstance(plan_id, int) and isinstance(sim_id, int)

    stored_run = repo.get_run(run.run_id)
    assert stored_run is not None
    plans = repo.list_plans(run.run_id)
    assert len(plans) == 1
    assert plans[0]["n_group0"] == 25
    assert json.loads(plans[0]["request_json"])["effect"] == 0.8


@pytest.mark.integration
def test_repository_persists_failed_plan_as_not_success(tmp_path) -> None:
    repo = RunRepository(tmp_path / "db" / "fail.db")
    run = RunIdentity.new("persist-fail")
    repo.start_run(run.run_id, run.created_at, "plan-binomial", run.versions)
    result = {"success": False, "failure_category": "effect_zero",
              "n_per_group0": None, "n_per_group1": None, "n_total": None,
              "achieved_power": None, "method": None}
    repo.save_plan(run.run_id, "binomial", {"p0": 0.3, "p1": 0.3}, result)
    plan = repo.list_plans(run.run_id)[0]
    assert plan["success"] == 0
    assert plan["failure_category"] == "effect_zero"
