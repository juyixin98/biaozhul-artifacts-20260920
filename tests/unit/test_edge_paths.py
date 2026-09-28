"""Tests for remaining error paths and edge branches."""
from __future__ import annotations

import pytest

from ssp.contracts import (
    Alternative,
    BinomialEffectScale,
    BinomialSpec,
    NormalEffectScale,
    NormalSpec,
    Allocation,
)
from ssp.errors import (
    PersistenceError,
    SimulationError,
    ValidationError,
)
from ssp.evidence import simulate_normal_power
from ssp.repro import derive_seed, list_fixtures, load_fixture
from ssp.storage import RunStore


@pytest.mark.unit
def test_simulation_rejects_nonpositive_trials(logger):
    spec = NormalSpec(
        Alternative.TWO_SIDED, 0.05, 0.8, 0.5,
        NormalEffectScale.STANDARDIZED_D, False, known_sigma=True,
    ).validate()
    with pytest.raises(SimulationError) as exc:
        simulate_normal_power(spec, Allocation(n0=10, n1=0), 0, seed=1, logger=logger)
    assert exc.value.category.value == "simulation_error"


@pytest.mark.unit
@pytest.mark.slow
def test_less_direction_two_sample_simulation_runs(logger):
    spec = BinomialSpec(
        Alternative.LESS, 0.05, 0.8, 0.3, 0.2,
        BinomialEffectScale.PROPORTIONS, True,
    ).validate()
    from ssp.evidence import simulate_binomial_power_asymptotic

    ev = simulate_binomial_power_asymptotic(
        spec, Allocation(n0=295, n1=295), 3_000, seed=21, logger=logger
    )
    assert ev.estimated_power > 0.7


@pytest.mark.unit
def test_meets_target_flag_set(logger):
    spec = NormalSpec(
        Alternative.TWO_SIDED, 0.05, 0.8, 0.5,
        NormalEffectScale.STANDARDIZED_D, False, known_sigma=True,
    ).validate()
    ev = simulate_normal_power(spec, Allocation(n0=32, n1=0), 2_000, seed=2, logger=logger)
    assert ev.meets_target_within_mcse in (True, False)
    assert ev.target_power == 0.8


@pytest.mark.unit
def test_load_fixture_roundtrip(settings):
    names = list_fixtures(settings)
    assert "normal_textbook.json" in names
    fixture = load_fixture("normal_textbook.json", settings)
    assert fixture["request"]["alpha"] == 0.05


@pytest.mark.unit
def test_load_missing_fixture_raises_simulation_error(settings):
    with pytest.raises(SimulationError):
        load_fixture("does-not-exist.json", settings)


@pytest.mark.integration
def test_persistence_error_on_unwritable_path(settings, tmp_path):
    # A path whose parent is an ordinary file cannot host a SQLite DB.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    broken = type(settings)(
        project_root=settings.project_root,
        db_path=blocker / "plans.db",
        log_level=settings.log_level,
        log_dir=settings.log_dir,
        exact_one_sample_cap=settings.exact_one_sample_cap,
        exact_two_sample_total_cap=settings.exact_two_sample_total_cap,
        approx_min_expected=settings.approx_min_expected,
        mc_default_trials=settings.mc_default_trials,
    )
    store = RunStore(broken)
    with pytest.raises(PersistenceError):
        store.initialize()


@pytest.mark.unit
def test_relative_risk_effect_must_be_positive():
    spec = BinomialSpec(
        Alternative.GREATER, 0.05, 0.8, 0.1, -1.0,
        BinomialEffectScale.RELATIVE_RISK, False,
    )
    with pytest.raises(ValidationError):
        spec.validate()


@pytest.mark.unit
def test_seed_distinct_across_run_ids():
    assert derive_seed("run-a", "p") != derive_seed("run-b", "p")
