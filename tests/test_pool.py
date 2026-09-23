"""Unit tests for the pool mathematics (app.pool)."""

import numpy as np
import pytest

from app.pool import (
    A_MAX,
    A_MIN,
    PRECISION,
    PoolStateError,
    compute_d,
    denormalize,
    get_y,
    get_y_bisection,
    invariant_residual_exact,
    normalize,
)

A = 100  # amplification used unless a test says otherwise


# ---------------------------------------------------------------- D solve

def test_d_balanced_pool_is_exact():
    # For x == y == k the invariant is satisfied exactly by D = 2k.
    k = 10**6 * PRECISION
    r = compute_d(k, k, A)
    assert r.converged
    assert r.value == 2 * k
    assert r.residual <= 1


def test_d_zero_reserves_rejected():
    with pytest.raises(PoolStateError):
        compute_d(0, 0, A)
    with pytest.raises(PoolStateError):
        compute_d(0, 10 * PRECISION, A)
    with pytest.raises(PoolStateError):
        compute_d(10 * PRECISION, 0, A)


def test_d_amplification_bounds_accepted():
    k = 10**4 * PRECISION
    assert compute_d(k, k, A_MIN).converged
    assert compute_d(k, k, A_MAX).converged
    with pytest.raises(PoolStateError):
        compute_d(k, k, A_MIN - 1)
    with pytest.raises(PoolStateError):
        compute_d(k, k, A_MAX + 1)


def test_d_iteration_limit_reports_non_convergence():
    # A budget of 1 iteration cannot converge from the initial guess.
    r = compute_d(10**6 * PRECISION, 7 * 10**5 * PRECISION, A, max_iter=1)
    assert not r.converged
    assert r.iterations == 1


def test_d_limit_cycle_handled():
    # Regression: for (1e24, 1) floor division traps plain Newton in a
    # period-2 limit cycle. The solver must detect it and return the
    # cycle member with the smallest invariant residual.
    r = compute_d(10**24, 1, A)
    assert r.converged
    assert r.converged_via == "cycle"
    # Residual is negligible relative to the invariant scale (Ann*S ~ 4e26).
    assert r.residual * 10**9 < 4 * A * (10**24 + 1)
    # And the solved D still lets get_y recover the tiny balance.
    y = get_y(10**24, r.value, A)
    assert y.converged
    assert abs(y.value - 1) <= 1


# ---------------------------------------------------------------- y solve

def test_get_y_roundtrip_recovers_balance():
    x, y = 3 * 10**5 * PRECISION, 9 * 10**5 * PRECISION
    d = compute_d(x, y, A).value
    r = get_y(x, d, A)
    assert r.converged
    assert abs(r.value - y) <= 2


def test_get_y_extreme_imbalance():
    # 1 unit against 10**30 units of normalised balance. In this corner the
    # invariant is ill-conditioned in y given D (dy/dD ~ 1e9), so the check
    # is on the relative error and the invariant residual, not absolute wei.
    x, y = 1, 10**30
    d = compute_d(x, y, A).value
    r = get_y(x, d, A)
    assert r.converged
    assert abs(r.value - y) / y < 1e-15
    res = invariant_residual_exact(x, r.value, d, A)
    assert res * 10**15 < d


def test_get_y_iteration_limit_reports_non_convergence():
    x, y = 10**6 * PRECISION, 10**6 * PRECISION
    d = compute_d(x, y, A).value
    r = get_y(x + 10**24, d, A, max_iter=1)
    assert not r.converged
    assert r.iterations == 1


# ------------------------------------------- Newton vs bisection reference

@pytest.mark.parametrize("x,y,a", [
    (10**6 * PRECISION, 10**6 * PRECISION, 1),
    (10**6 * PRECISION, 10**6 * PRECISION, 5000),
    (10**6 * PRECISION, 10**3 * PRECISION, 100),
    (1, 10**30, 100),
    (10**30, 1, 7),
    (123456789 * PRECISION, 987654321 * PRECISION, 250),
])
def test_newton_agrees_with_bisection(x, y, a):
    d = compute_d(x, y, a).value
    dx = max(1, x // 10)
    newton = get_y(x + dx, d, a)
    bisect = get_y_bisection(x + dx, d, a)
    assert newton.converged and bisect.converged
    assert abs(newton.value - bisect.value) <= 4


def test_bisection_iteration_limit_reports_non_convergence():
    x, y = 10**6 * PRECISION, 10**6 * PRECISION
    d = compute_d(x, y, A).value
    r = get_y_bisection(x + 10**24, d, A, max_iter=2)
    assert not r.converged


# ------------------------------------------------------- invariant quality

def test_invariant_residual_after_swap_is_tiny():
    x, y = 10**6 * PRECISION, 10**6 * PRECISION
    d = compute_d(x, y, A).value
    x2 = x + 10**5 * PRECISION
    y2 = get_y(x2, d, A).value
    res = invariant_residual_exact(x2, y2, d, A)
    # Residual is far below one part in 10**20 of D.
    assert res * 10**20 < d


def test_invariant_residual_randomised_numpy():
    # Vectorised randomised check: post-swap residual stays negligible
    # across random pool shapes and amplifications.
    rng = np.random.default_rng(20260922)
    for _ in range(50):
        x = int(rng.integers(1, 10**9)) * PRECISION
        y = int(rng.integers(1, 10**9)) * PRECISION
        a = int(rng.integers(A_MIN, A_MAX + 1))
        d = compute_d(x, y, a).value
        dx = int(rng.integers(1, 10**9)) * PRECISION
        y2 = get_y(x + dx, d, a).value
        res = invariant_residual_exact(x + dx, y2, d, a)
        assert res * 10**18 < d, f"x={x} y={y} a={a} dx={dx}"


# ------------------------------------------------------- normalisation I/O

def test_normalize_denormalize_roundtrip():
    assert normalize(1_000_000, 6) == 10**18  # 1.0 of a 6-decimal token
    assert denormalize(10**18, 6) == 1_000_000
    assert normalize(5, 18) == 5
    assert denormalize(5, 18) == 5


def test_denormalize_floors_dust():
    # 1.5 tokens of a 6-decimal asset plus sub-unit dust -> dust discarded.
    assert denormalize(15 * 10**17 + 10**11, 6) == 1_500_000
    assert denormalize(10**18 - 1, 0) == 0  # sub-unit dust discarded


def test_small_input_one_wei():
    x = y = 10**6 * PRECISION
    d = compute_d(x, y, A).value
    r = get_y(x + 1, d, A)  # dx = 1 normalised wei
    assert r.converged
    dy = y - r.value
    assert 0 <= dy <= 1
