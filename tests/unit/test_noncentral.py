"""Unit tests for the non-central distribution kernels.

Reference values are computed inline from raw scipy/numpy formulas (or exact
binomial sums), never by calling the solver or the function under test.
"""
from __future__ import annotations

import math

import pytest
from scipy.stats import binom, nct, norm

from sample_size_planner.estimation import noncentral as nc


# --------------------------------------------------------------------------- #
# Critical values -- independent analytic anchors
# --------------------------------------------------------------------------- #
def test_z_critical_matches_scipy() -> None:
    assert nc.z_critical(0.05, two_sided=True) == pytest.approx(norm.ppf(0.975), abs=1e-12)
    assert nc.z_critical(0.05, two_sided=False) == pytest.approx(norm.ppf(0.95), abs=1e-12)


# --------------------------------------------------------------------------- #
# Normal power: independent reference via explicit normal CDF
# --------------------------------------------------------------------------- #
def _ref_two_sample_z_power(n0, n1, d, a, two_sided):
    se = math.sqrt(2.0 / n0) if n1 == n0 else math.sqrt(1.0 / n0 + 1.0 / n1)
    lam = d / se
    zc = norm.isf(a / 2 if two_sided else a)
    if two_sided:
        return norm.sf(zc - lam) + norm.cdf(-zc - lam)
    return norm.sf(zc - lam)


@pytest.mark.normal
def test_normal_power_matches_independent_z_formula() -> None:
    for (n0, d, two) in [(25, 0.8, True), (20, 0.8, False), (40, 0.5, True), (32, 0.5, False)]:
        got = nc.normal_power(n0, n0, d, 1.0, 1.0, 0.05, two, 1.0, use_t=False)
        expected = _ref_two_sample_z_power(n0, n0, d, 0.05, two)
        assert got == pytest.approx(expected, abs=1e-12)


@pytest.mark.normal
def test_normal_power_at_h0_equals_alpha() -> None:
    # under the null (effect 0) the rejection probability is exactly the size
    got = nc.normal_power(100, 100, 0.0, 1.0, 1.0, 0.05, True, 1.0, use_t=False)
    assert got == pytest.approx(0.05, abs=1e-9)


@pytest.mark.normal
def test_noncentral_t_power_matches_scipy_nct() -> None:
    n0, n1, d = 26, 26, 0.8
    df = n0 + n1 - 2
    se = math.sqrt(1.0 / n0 + 1.0 / n1)
    lam = d / se
    tc = __import__("scipy.stats", fromlist=["t"]).t.ppf(0.975, df)
    expected = float(nct.sf(tc, df, lam) + nct.cdf(-tc, df, lam))
    got = nc.normal_power(n0, n1, d, 1.0, 1.0, 0.05, True, 1.0, use_t=True)
    assert got == pytest.approx(expected, abs=1e-10)


@pytest.mark.normal
def test_continuous_root_recovers_hand_formula() -> None:
    # n per group = 2 (z.975 + z.8)^2 / d^2 for a balanced two-sided z test
    zc, zb = norm.ppf(0.975), norm.ppf(0.8)
    expected = 2.0 * (zc + zb) ** 2 / 0.64
    got = nc.normal_n0_continuous(0.8, 1.0, 1.0, 0.05, 0.8, True, 1.0, False)
    assert got == pytest.approx(expected, rel=1e-12)


# --------------------------------------------------------------------------- #
# One-sample exact binomial: hand-derived critical region
# --------------------------------------------------------------------------- #
@pytest.mark.binomial
def test_exact_one_sample_n20_hand_case() -> None:
    # n=20, H0 p=.5, one-sided alpha=.05: reject iff X >= 15 because
    # P_.5(X>=15)=0.02069 <= .05 while P_.5(X>=14)=0.05766 > .05.
    assert binom.sf(14, 20, 0.5) == pytest.approx(0.0206947326660156, rel=1e-9)
    assert binom.sf(13, 20, 0.5) == pytest.approx(0.0576591491699219, rel=1e-9)
    size = nc.binomial_power_exact_one_sample(20, 0.5, 0.5, 0.05, False, True)
    assert size == pytest.approx(0.0206947326660156, abs=1e-9)
    # power against p1=.7: P_.7(X>=15)
    power = nc.binomial_power_exact_one_sample(20, 0.5, 0.7, 0.05, False, True)
    assert power == pytest.approx(binom.sf(14, 20, 0.7), abs=1e-12)
    assert power == pytest.approx(0.4163708294474812, abs=1e-10)


@pytest.mark.binomial
def test_exact_one_sample_lower_tail() -> None:
    # lower-tail test, n=20, H0 .5: reject X <= 5 (tail .0207)
    power = nc.binomial_power_exact_one_sample(20, 0.5, 0.3, 0.05, False, False)
    assert power == pytest.approx(binom.cdf(5, 20, 0.3), abs=1e-12)


@pytest.mark.binomial
def test_exact_one_sample_two_sided_conservative_size() -> None:
    size = nc.binomial_power_exact_one_sample(50, 0.3, 0.3, 0.05, True, True)
    assert size <= 0.05 + 1e-12


# --------------------------------------------------------------------------- #
# Two-sample exact score power: independent enumeration for a tiny case
# --------------------------------------------------------------------------- #
@pytest.mark.binomial
def test_exact_two_sample_matches_brute_force_enumeration() -> None:
    # Tiny n so every (x0,x1) pair can be enumerated by hand independently.
    n0, n1, p0, p1 = 8, 6, 0.25, 0.6
    zc = norm.isf(0.05)
    expected = 0.0
    for x0 in range(n0 + 1):
        for x1 in range(n1 + 1):
            pbar = (x0 + x1) / (n0 + n1)
            var = pbar * (1 - pbar) * (1 / n0 + 1 / n1)
            if var <= 0:
                reject = False
            else:
                z = (x1 / n1 - x0 / n0) / math.sqrt(var)
                reject = z >= zc
            if reject:
                expected += binom.pmf(x0, n0, p0) * binom.pmf(x1, n1, p1)
    got = nc.binomial_power_exact_two_sample(n0, n1, p0, p1, 0.05, False, True)
    assert got == pytest.approx(expected, abs=1e-10)


@pytest.mark.binomial
def test_exact_two_sample_size_under_null_bounded_by_alpha() -> None:
    # At adequate expected counts the (exactly evaluated) score test holds its
    # nominal size.
    size = nc.binomial_power_exact_two_sample(50, 50, 0.3, 0.3, 0.05, True, True)
    assert size <= 0.05 + 1e-9

    # At *sparse* counts the asymptotic score test is mildly anti-conservative;
    # the exact enumerator reports that honestly rather than hiding it. Here
    # n1*p = 60*0.1 = 6 is inside the low-base-rate regime the planner guards.
    sparse_size = nc.binomial_power_exact_two_sample(40, 60, 0.1, 0.1, 0.05, True, True)
    assert 0.05 - 0.005 < sparse_size <= 0.05 + 0.005
    assert 60 * 0.1 < 5.0 + 2.0  # within the low-rate guard band


@pytest.mark.binomial
def test_low_rate_planning_does_not_use_approx_guard_band() -> None:
    # The very sparse scenario above must be flagged low-base-rate by the
    # solver (so planning switches to exact enumeration).
    from sample_size_planner.contracts import BinomialSpec, TestDirection
    from sample_size_planner.estimation.solvers import SolverConfig, solve_binomial

    result = solve_binomial(
        BinomialSpec(0.1, 0.3, 0.05, 0.8, TestDirection.GREATER),
        SolverConfig(),
    )
    assert result.diagnostics["low_base_rate"] is True
    assert result.method.value == "exact_binomial"


@pytest.mark.binomial
def test_binomial_continuous_root_closed_form() -> None:
    # independent closed-form for one-sample proportion
    zc, zb, p0, p1 = norm.ppf(0.975), norm.ppf(0.8), 0.5, 0.7
    expected = (zc * math.sqrt(p0 * 0.5) + zb * math.sqrt(p1 * 0.3)) ** 2 / (0.2 ** 2)
    got = nc.binomial_n0_continuous(p0, p1, 0.05, 0.8, True, 1.0, True)
    assert got == pytest.approx(expected, rel=1e-12)
