"""Integration tests driven by the shipped synthetic fixtures.

Each fixture carries an independently computed reference block; these tests
run the full experiment (planner + seeded Monte-Carlo evidence + persistence)
and assert the concrete reference numbers, the n-passes/n-minus-one-fails
boundary, and analytic-vs-simulation agreement.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ssp.api.schemas import BinomialPlanRequest, NormalPlanRequest
from ssp.api.service import PlanningService
from ssp.diagnostics import RunLogger, input_fingerprint
from ssp.repro import ExperimentRunner
from ssp.storage import RunStore

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "data" / "fixtures"


def _load(name: str) -> dict:
    with (FIXTURE_DIR / name).open("r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture()
def runner(settings) -> ExperimentRunner:
    RunStore(settings).initialize()
    return ExperimentRunner(settings)


def _run_fixture(fixture: dict, runner: ExperimentRunner, name: str, trials: int = 8_000):
    request = fixture["request"]
    # Deterministic run id per fixture file -> deterministic derived RNG seed,
    # so the agreement assertion is reproducible rather than seed-lottery.
    run_id = f"itest-{name.removesuffix('.json')}"
    fp = input_fingerprint(request)
    logger = RunLogger(run_id, fp)
    if request["endpoint"] == "normal":
        from ssp.api.service import build_normal_spec

        spec = build_normal_spec(NormalPlanRequest(**request))
        return runner.run_normal(spec, run_id, fp, logger, trials=trials)
    from ssp.api.service import build_binomial_spec

    spec = build_binomial_spec(BinomialPlanRequest(**request))
    return runner.run_binomial(spec, run_id, fp, logger, trials=trials)


NORMAL_FIXTURES = ["normal_textbook.json", "normal_t_small.json", "normal_two_sample_d20.json"]
BINOMIAL_FIXTURES = [
    "binomial_low_rate_exact.json",
    "binomial_moderate.json",
    "binomial_fisher.json",
]


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize("name", NORMAL_FIXTURES)
def test_normal_fixtures_match_reference(name, runner):
    fixture = _load(name)
    record = _run_fixture(fixture, runner, name)
    plan, ref = record.plan, fixture["reference"]

    assert plan.allocation.total == ref["n_total"]
    assert plan.achieved_power == pytest.approx(ref["power_at_n"], abs=2e-3)
    assert plan.power_at_total_minus_one == pytest.approx(
        ref["power_at_n_minus_one"], abs=2e-3
    )
    assert plan.is_committed()
    assert record.agreement["agrees"] is True


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize("name", BINOMIAL_FIXTURES)
def test_binomial_fixtures_match_reference(name, runner):
    from reference_oracle import score_statistic_power_two

    fixture = _load(name)
    record = _run_fixture(fixture, runner, name)
    plan, ref = record.plan, fixture["reference"]

    assert plan.allocation.n0 == ref["n0"]
    if "n1" in ref:
        assert plan.allocation.n1 == ref["n1"]
    assert plan.method == ref["method"]
    assert plan.achieved_power == pytest.approx(ref["power_at_n"], abs=2e-3)
    assert plan.power_at_total_minus_one < plan.target_power
    assert plan.is_committed()

    allowance = ref.get("approximation_allowance")
    if allowance is not None:
        # Asymptotic two-sample plan: analytic power is a fixed-SE approximation;
        # the independent evidence must instead match exact enumeration of the
        # actual score statistic, and the approximation gap is bounded/documented.
        operational = score_statistic_power_two(
            plan.allocation.n0, plan.allocation.n1,
            plan.spec["p0"], plan.spec["p1"], plan.alpha, plan.alternative.value,
        )
        assert record.evidence.ci95_low <= operational <= record.evidence.ci95_high
        assert abs(plan.achieved_power - operational) <= allowance
    else:
        assert record.agreement["agrees"] is True


@pytest.mark.integration
def test_failure_run_is_persisted_with_category_not_success(settings):
    store = RunStore(settings)
    store.initialize()
    service = PlanningService(store=store)
    from ssp.errors import ValidationError

    payload = {
        "endpoint": "normal", "alternative": "two_sided", "alpha": 0.05,
        "target_power": 0.8, "effect": 0.0, "effect_scale": "standardized_d",
        "two_sample": False,
    }
    with pytest.raises(ValidationError):
        service.plan_normal(NormalPlanRequest(**payload))
    runs = store.list_runs()
    assert runs and runs[0]["status"] == "failed"
    assert runs[0]["error_category"] == "validation_error"
    assert runs[0]["result"] is None


@pytest.mark.integration
def test_versions_and_fingerprint_recorded(settings):
    store = RunStore(settings)
    store.initialize()
    service = PlanningService(store=store)
    payload = {
        "endpoint": "normal", "alternative": "greater", "alpha": 0.05,
        "target_power": 0.8, "effect": 0.5, "effect_scale": "standardized_d",
        "two_sample": False, "known_sigma": True, "mc_trials": 500,
    }
    record = service.plan_normal(NormalPlanRequest(**payload))
    row = store.get(record["run_id"])
    assert row["fingerprint"] == record["input_fingerprint"]
    assert row["versions"]["numpy"]
    assert row["versions"]["scipy"]
    # Same inputs -> same fingerprint.
    fp1 = input_fingerprint(payload)
    fp2 = input_fingerprint(dict(payload))
    assert fp1 == fp2
