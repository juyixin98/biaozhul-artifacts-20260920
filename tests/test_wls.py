"""Tests for the SVD weighted ridge solver."""

import numpy as np
import pytest

from robustreg.wls import RankDeficientError, weighted_ridge_solve


def test_ols_matches_numpy_lstsq():
    rng = np.random.default_rng(0)
    A = rng.normal(size=(50, 3))
    y = rng.normal(size=50)
    beta = weighted_ridge_solve(A, y)["beta"]
    ref, *_ = np.linalg.lstsq(A, y, rcond=None)
    np.testing.assert_allclose(beta, ref, rtol=1e-11, atol=1e-12)


def test_weighted_solve_matches_known_formula():
    # Weighted LS via stack must equal (A'WA)^-1 A'Wy on a full-rank case.
    rng = np.random.default_rng(1)
    A = rng.normal(size=(40, 2))
    y = rng.normal(size=40)
    w = rng.uniform(0.1, 2.0, size=40)
    sol = weighted_ridge_solve(A, y, w=w)
    W = np.diag(w)
    ref = np.linalg.solve(A.T @ W @ A, A.T @ W @ y)
    np.testing.assert_allclose(sol["beta"], ref, rtol=1e-10, atol=1e-12)
    assert sol["rank"] == 2
    assert sol["rank_deficient"] is False


def test_zero_weight_rows_are_removed():
    # Two contradictory points; giving one zero weight must fully ignore it.
    A = np.array([[1.0], [1.0]])
    y = np.array([3.0, 9.0])
    sol = weighted_ridge_solve(A, y, w=np.array([1.0, 0.0]))
    assert sol["beta"] == pytest.approx(3.0)
    sol2 = weighted_ridge_solve(A, y, w=np.array([0.0, 1.0]))
    assert sol2["beta"] == pytest.approx(9.0)


def test_all_zero_weights_without_penalty_raises():
    with pytest.raises(ValueError, match="unconstrained"):
        weighted_ridge_solve(np.eye(2), np.array([1.0, 2.0]),
                             w=np.zeros(2))


def test_intercept_unpenalized_but_slopes_penalized():
    # With penalty on slopes, the intercept-only direction must stay free:
    # compare against a direct stacked solve built independently.
    rng = np.random.default_rng(2)
    n, p = 30, 2
    X = rng.normal(size=(n, p))
    A = np.hstack([np.ones((n, 1)), X])
    y = 4.0 + X @ np.array([1.0, -1.0]) + rng.normal(scale=0.01, size=n)
    lam = 5.0
    pen = np.array([0.0, lam, lam])
    sol = weighted_ridge_solve(A, y, pen=pen)
    ref, *_ = np.linalg.lstsq(
        np.vstack([A, np.diag(np.sqrt(pen))]),
        np.concatenate([y, np.zeros(3)]),
        rcond=None,
    )
    np.testing.assert_allclose(sol["beta"], ref, rtol=1e-11, atol=1e-12)
    assert sol["beta"][0] == pytest.approx(4.0, abs=0.05)


def test_exact_collinear_columns_raise():
    A = np.array([[1.0, 2.0], [2.0, 4.0], [3.0, 6.0], [4.0, 8.0]])
    y = np.array([1.0, 3.0, 2.0, 5.0])
    with pytest.raises(RankDeficientError) as exc:
        weighted_ridge_solve(A, y)
    assert exc.value.rank == 1
    assert exc.value.nullspace_dim == 1


def test_near_collinear_columns_detected_with_tight_rcond():
    rng = np.random.default_rng(3)
    x = rng.normal(size=40)
    A = np.column_stack([x, x + 1e-13 * rng.normal(size=40)])
    y = rng.normal(size=40)
    with pytest.raises(RankDeficientError):
        weighted_ridge_solve(A, y, rcond=1e-10)


def test_allow_rank_deficient_returns_minimum_norm():
    A = np.array([[1.0, 1.0], [1.0, 1.0], [1.0, 1.0]])
    y = np.array([2.0, 2.0, 2.0])
    sol = weighted_ridge_solve(A, y, allow_rank_deficient=True)
    assert sol["rank_deficient"] is True
    assert sol["rank"] == 1
    # Minimum-norm solution splits the fit equally.
    np.testing.assert_allclose(sol["beta"], [1.0, 1.0], atol=1e-12)


def test_underdetermined_system_raises():
    rng = np.random.default_rng(4)
    A = rng.normal(size=(3, 5))
    with pytest.raises(RankDeficientError) as exc:
        weighted_ridge_solve(A, rng.normal(size=3))
    assert exc.value.p == 5
    assert exc.value.nullspace_dim == 2


def test_ridge_penalty_resolves_deficiency():
    # Collinear slopes become full-rank when penalized (intercept stays free,
    # but the null direction lives entirely in the slopes).
    rng = np.random.default_rng(5)
    x = rng.normal(size=50)
    A = np.column_stack([np.ones(50), x, x])
    y = 2.0 + x + rng.normal(scale=0.05, size=50)
    sol = weighted_ridge_solve(A, y, pen=np.array([0.0, 1e-6, 1e-6]))
    assert sol["rank"] == 3
    assert sol["rank_deficient"] is False


def test_negative_weights_rejected():
    with pytest.raises(ValueError, match="non-negative"):
        weighted_ridge_solve(np.eye(2), np.zeros(2), w=np.array([1.0, -1.0]))
