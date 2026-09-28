"""Tests for the sample-size solvers.

The headline acceptance condition is asserted for every successful result:
    power(n) >= target  AND  power(n - 1) < target.
Zero effect, low-base-rate approximation failure and the size cap are asserted
as their explicit failure categories -- never as success.
"""
from __future__ import annotations

import pytest

from sample_size_planner.contracts import (
    BinomialSpec,
    FailureCategory,
    NormalSpec,
    SolverMethod,
    TestDirection,
)
from sample_size_planner.estimation import noncentral as nc
from sample_size_planner.estimation.solvers import SolverConfig, solve_binomial, solve_normal


def _assert_boundary(result, target: float) -> None:
    assert result.success
    assert result.ok
    assert result.achieved_power >= target
    assert result.power_at_n_minus_one is not None
    assert result.power_at_n_minus_one < target


# --------------------------------------------------------------------------- #
# Normal solver -- concrete textbook answers
# --------------------------------------------------------------------------- #
@pytest.mark.normal
def test_normal_cohen_d80_two_sided_is_25_per_group() -> None:
    spec = NormalSpec(0.05, 0.8, TestDirection.TWO_SIDED, standardized_effect=0.8)
    result = solve_normal(spec)
    _assert_boundary(result, 0.8)
    assert result.n_per_group0 == 25
    assert result.n_per_group1 == 25
    assert result.n_total == 50
    assert result.method is SolverMethod.NORMAL_APPROX


@pytest.mark.normal
def test_normal_t_distribution_needs_one_more_per_group() -> None:
    spec = NormalSpec(0.05, 0.8, TestDirection.TWO_SIDED, standardized_effect=0.8,
                      use_t_distribution=True)
    result = solve_normal(spec)
    _assert_boundary(result, 0.8)
    assert result.n_per_group0 == 26
    assert result.method is SolverMethod.STUDENT_T


@pytest.mark.normal
def test_normal_one_sample_is_32() -> None:
    spec = NormalSpec(0.05, 0.8, TestDirection.TWO_SIDED, standardized_effect=0.5,
                      one_sample=True)
    result = solve_normal(spec)
    _assert_boundary(result, 0.8)
    assert result.n_per_group0 == 32
    assert result.n_per_group1 is None
    assert result.n_total == 32


@pytest.mark.normal
def test_normal_one_sided_is_20_per_group() -> None:
    spec = NormalSpec(0.05, 0.8, TestDirection.GREATER, standardized_effect=0.8)
    result = solve_normal(spec)
    _assert_boundary(result, 0.8)
    assert result.n_per_group0 == 20


@pytest.mark.normal
def test_normal_allocation_ratio_2_expands_reference_arm() -> None:
    spec = NormalSpec(0.05, 0.8, TestDirection.TWO_SIDED, allocation_ratio=2.0,
                      standardized_effect=0.5)
    result = solve_normal(spec)
    _assert_boundary(result, 0.8)
    assert result.n_per_group1 == pytest.approx(2 * result.n_per_group0, abs=1)
    assert result.n_per_group0 == 48 and result.n_per_group1 == 96


@pytest.mark.normal
def test_normal_zero_effect_is_explicit_failure() -> None:
    spec = NormalSpec(0.05, 0.8, TestDirection.TWO_SIDED, standardized_effect=0.0)
    result = solve_normal(spec)
    assert not result.success
    assert result.failure_category is FailureCategory.EFFECT_ZERO
    assert result.n_total is None
    # and the diagnostics state the statistical reason
    assert result.diagnostics["limit_power_as_n_to_inf"] == pytest.approx(0.05)


@pytest.mark.normal
def test_normal_cap_breach_is_reported_not_truncated() -> None:
    spec = NormalSpec(0.05, 0.8, TestDirection.TWO_SIDED, standardized_effect=1e-4)
    result = solve_normal(spec, SolverConfig(max_sample_size=1000))
    assert not result.success
    assert result.failure_category is FailureCategory.EXACT_LIMITED_BY_CAP


@pytest.mark.normal
def test_normal_invalid_power_below_alpha_rejected_at_contract() -> None:
    with pytest.raises(ValueError, match="power"):
        NormalSpec(0.05, 0.02, TestDirection.TWO_SIDED, standardized_effect=0.8)


@pytest.mark.normal
def test_normal_less_direction_matches_greater_by_symmetry() -> None:
    greater = solve_normal(NormalSpec(0.05, 0.8, TestDirection.GREATER,
                                      standardized_effect=0.8))
    less = solve_normal(NormalSpec(0.05, 0.8, TestDirection.LESS,
                                   standardized_effect=-0.8))
    _assert_boundary(less, 0.8)
    assert less.n_per_group0 == greater.n_per_group0
    assert less.achieved_power == pytest.approx(greater.achieved_power, abs=1e-12)


# --------------------------------------------------------------------------- #
# Binomial solver
# --------------------------------------------------------------------------- #
@pytest.mark.binomial
def test_binomial_moderate_rates_normal_answer_with_boundary() -> None:
    spec = BinomialSpec(0.5, 0.7, 0.05, 0.8, TestDirection.TWO_SIDED)
    result = solve_binomial(spec)
    _assert_boundary(result, 0.8)
    assert result.n_per_group0 == 96
    assert result.method is SolverMethod.NORMAL_APPROX
    # the exact cross-check was performed and is close
    assert result.diagnostics["exact_cross_check_power_at_n"] == pytest.approx(
        result.achieved_power, abs=0.02)


@pytest.mark.binomial
def test_binomial_low_base_rate_switches_to_exact() -> None:
    spec = BinomialSpec(0.001, 0.01, 0.05, 0.8, TestDirection.GREATER)
    result = solve_binomial(spec)
    _assert_boundary(result, 0.8)
    assert result.method is SolverMethod.EXACT_BINOMIAL
    assert result.diagnostics["low_base_rate"] is True
    assert result.diagnostics["minimum_expected_count"] < 5.0
    assert "approximation_switch_reason" in result.diagnostics


@pytest.mark.binomial
def test_binomial_extreme_high_rate_backtracks_to_minimum() -> None:
    # Normal root grossly overshoots; exact power with backtrack returns the
    # smallest integer, and n-1 genuinely fails.
    spec = BinomialSpec(0.99, 0.999, 0.05, 0.8, TestDirection.GREATER)
    result = solve_binomial(spec)
    _assert_boundary(result, 0.8)
    assert result.method is SolverMethod.EXACT_BINOMIAL
    assert result.n_per_group0 < 1000  # the naive normal root was ~1259


@pytest.mark.binomial
def test_binomial_rare_event_strict_boundary_high_precision() -> None:
    spec = BinomialSpec(1e-4, 3e-4, 0.05, 0.8, TestDirection.GREATER)
    result = solve_binomial(spec, SolverConfig(max_sample_size=10_000_000))
    _assert_boundary(result, 0.8)
    assert result.achieved_power - 0.8 >= 0.0
    assert result.power_at_n_minus_one < 0.8


@pytest.mark.binomial
def test_binomial_one_sample_exact_hand_scenario() -> None:
    spec = BinomialSpec(0.5, 0.7, 0.05, 0.8, TestDirection.TWO_SIDED, one_sample=True)
    result = solve_binomial(spec)
    _assert_boundary(result, 0.8)
    assert result.n_per_group0 == 49
    assert result.n_per_group1 is None
    assert result.method is SolverMethod.EXACT_BINOMIAL


@pytest.mark.binomial
def test_binomial_normal_under_delivery_triggers_exact_fallback() -> None:
    # .10 vs .20 one-sided: the normal answer's *exact* power is only ~0.73,
    # so the cross-check must demote it and re-solve exactly.
    spec = BinomialSpec(0.10, 0.20, 0.05, 0.8, TestDirection.GREATER)
    result = solve_binomial(spec)
    _assert_boundary(result, 0.8)
    assert result.method is SolverMethod.EXACT_BINOMIAL
    assert result.n_per_group0 > 133  # exact needs more than the normal root
    assert "fallback_trigger" in result.diagnostics


@pytest.mark.binomial
def test_binomial_zero_effect_two_sided_is_explicit_failure() -> None:
    spec = BinomialSpec(0.3, 0.3, 0.05, 0.8, TestDirection.TWO_SIDED)
    result = solve_binomial(spec)
    assert not result.success
    assert result.failure_category is FailureCategory.EFFECT_ZERO


@pytest.mark.binomial
def test_binomial_direction_effect_mismatch_rejected() -> None:
    with pytest.raises(ValueError, match="greater"):
        BinomialSpec(0.7, 0.5, 0.05, 0.8, TestDirection.GREATER)


@pytest.mark.binomial
def test_binomial_less_direction_symmetric_with_greater() -> None:
    greater = solve_binomial(BinomialSpec(0.5, 0.7, 0.05, 0.8, TestDirection.GREATER))
    less = solve_binomial(BinomialSpec(0.5, 0.3, 0.05, 0.8, TestDirection.LESS))
    _assert_boundary(less, 0.8)
    assert less.n_per_group0 == greater.n_per_group0


@pytest.mark.binomial
def test_force_exact_bypasses_approximation_in_normal_regime() -> None:
    spec = BinomialSpec(0.2, 0.4, 0.05, 0.8, TestDirection.TWO_SIDED)
    result = solve_binomial(spec, SolverConfig(), force_exact=True)
    _assert_boundary(result, 0.8)
    assert result.method is SolverMethod.EXACT_BINOMIAL
    assert result.diagnostics["force_exact"] is True


@pytest.mark.binomial
def test_binomial_cap_breach_on_exact_path() -> None:
    spec = BinomialSpec(1e-6, 2e-6, 0.05, 0.99, TestDirection.GREATER)
    result = solve_binomial(spec, SolverConfig(max_sample_size=2000))
    assert not result.success
    assert result.failure_category is FailureCategory.EXACT_LIMITED_BY_CAP


# --------------------------------------------------------------------------- #
# Independent re-evaluation of the reported boundary through the exact kernel
# --------------------------------------------------------------------------- #
@pytest.mark.binomial
def test_reported_n_reconfirmed_by_independent_exact_call() -> None:
    spec = BinomialSpec(0.2, 0.4, 0.05, 0.8, TestDirection.TWO_SIDED)
    result = solve_binomial(spec, SolverConfig(max_sample_size=100_000,
                                               exact_verify_limit=100_000))
    n0 = result.n_per_group0
    p_n = nc.binomial_power_exact_two_sample(n0, n0, 0.2, 0.4, 0.05, True, True)
    p_nm1 = nc.binomial_power_exact_two_sample(n0 - 1, n0 - 1, 0.2, 0.4, 0.05, True, True)
    assert p_n >= 0.8 and p_nm1 < 0.8
