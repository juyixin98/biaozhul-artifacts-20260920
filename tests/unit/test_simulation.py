"""Tests for the independent Monte Carlo evidence layer.

The simulator is the external witness: it recomputes decisions from raw
synthetic draws and must agree with the analytic operating characteristic.
Reference targets are the planned/analytic power; the simulator never calls
the solver.
"""
from __future__ import annotations

import numpy as np
import pytest

from sample_size_planner.estimation import noncentral as nc
from sample_size_planner.evidence import simulation as sim


@pytest.mark.simulation
def test_normal_mc_agrees_with_analytic_power(rng) -> None:
    n0 = 25
    analytic = nc.normal_power(n0, n0, 0.8, 1.0, 1.0, 0.05, True, 1.0, use_t=False)
    out = sim.simulate_normal_power(
        n0=n0, n1=n0, effect=0.8, sd0=1.0, sd1=1.0, alpha=0.05,
        two_sided=True, use_t=False, replications=60_000, rng=rng)
    assert out.ci95_low <= analytic <= out.ci95_high
    assert abs(out.estimated_power - analytic) < 3 * out.standard_error


@pytest.mark.simulation
def test_normal_one_sample_mc_agrees_with_analytic_power(rng) -> None:
    # n=32, d=0.5 two-sided has analytic power ~0.807. Drawing only one
    # N(delta, sd) scalar per replicate (a real earlier bug) collapses power
    # toward ~0.55; this test pins the correct within-arm mean variance.
    n0 = 32
    analytic = nc.normal_power(n0, None, 0.5, 1.0, 1.0, 0.05, True, 1.0, use_t=False)
    out = sim.simulate_normal_power(
        n0=n0, n1=None, effect=0.5, sd0=1.0, sd1=1.0, alpha=0.05,
        two_sided=True, use_t=False, replications=60_000, rng=rng)
    assert out.ci95_low <= analytic <= out.ci95_high
    assert out.estimated_power > 0.75


@pytest.mark.simulation
def test_normal_zero_effect_mc_size_is_alpha(rng) -> None:
    out = sim.simulate_normal_power(
        n0=200, n1=200, effect=0.0, sd0=1.0, sd1=1.0, alpha=0.05,
        two_sided=True, use_t=False, replications=60_000, rng=rng)
    # no effect -> rejection rate must be the size alpha, not the target power
    assert out.ci95_low <= 0.05 <= out.ci95_high
    assert out.estimated_power < 0.1


@pytest.mark.simulation
def test_binomial_mc_agrees_with_exact_power_moderate(rng) -> None:
    n0 = 96
    exact = nc.binomial_power_exact_two_sample(n0, n0, 0.5, 0.7, 0.05, True, True)
    out = sim.simulate_binomial_power(
        n0=n0, n1=n0, p0=0.5, p1=0.7, alpha=0.05, two_sided=True,
        greater=True, exact_one_sample=False, replications=60_000, rng=rng)
    assert out.ci95_low <= exact <= out.ci95_high


@pytest.mark.simulation
def test_binomial_extreme_proportion_mc_matches_exact(rng) -> None:
    # rare-event one-sample design; the exact kernel and raw draws must agree.
    n0 = 299
    exact = nc.binomial_power_exact_one_sample(n0, 0.001, 0.01, 0.05, False, True)
    out = sim.simulate_binomial_power(
        n0=n0, n1=None, p0=0.001, p1=0.01, alpha=0.05, two_sided=False,
        greater=True, exact_one_sample=True, replications=100_000, rng=rng)
    assert out.ci95_low <= exact <= out.ci95_high


@pytest.mark.simulation
def test_binomial_mc_lower_tail_direction(rng) -> None:
    n0 = 60
    exact = nc.binomial_power_exact_one_sample(n0, 0.6, 0.4, 0.05, False, False)
    out = sim.simulate_binomial_power(
        n0=n0, n1=None, p0=0.6, p1=0.4, alpha=0.05, two_sided=False,
        greater=False, exact_one_sample=True, replications=60_000, rng=rng)
    assert out.ci95_low <= exact <= out.ci95_high


@pytest.mark.simulation
def test_wilson_interval_stays_in_unit_range_at_extremes(rng) -> None:
    # force a near-zero rejection proportion: tiny n and no effect one-sided
    out = sim.simulate_normal_power(
        n0=10, n1=10, effect=-3.0, sd0=1.0, sd1=1.0, alpha=0.05,
        two_sided=False, use_t=False, replications=20_000, rng=rng)
    assert 0.0 <= out.ci95_low <= out.ci95_high <= 1.0


@pytest.mark.simulation
def test_seed_makes_simulation_reproducible() -> None:
    a = sim.simulate_normal_power(n0=25, n1=25, effect=0.8, sd0=1.0, sd1=1.0,
                                  alpha=0.05, two_sided=True, use_t=False,
                                  replications=5000, rng=np.random.default_rng(99))
    b = sim.simulate_normal_power(n0=25, n1=25, effect=0.8, sd0=1.0, sd1=1.0,
                                  alpha=0.05, two_sided=True, use_t=False,
                                  replications=5000, rng=np.random.default_rng(99))
    assert a.rejections == b.rejections
    assert a.estimated_power == b.estimated_power
