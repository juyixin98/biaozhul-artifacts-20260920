"""Tests for Huber IRLS and the OLS baseline."""

import numpy as np
import pytest

from robustreg.huber import (
    huber_irls,
    huber_loss,
    huber_objective,
    huber_weight,
    ols_fit,
)
from robustreg.wls import RankDeficientError


@pytest.fixture
def outlier_data():
    rng = np.random.default_rng(2024)
    n = 100
    X = rng.normal(size=(n, 3))
    true_coef = np.array([2.0, -1.5, 0.7])
    y_clean = 1.0 + X @ true_coef
    noise = rng.normal(scale=0.3, size=n)
    y = y_clean + noise
    idx = rng.choice(n, size=10, replace=False)
    y[idx] += rng.choice([-1.0, 1.0], size=10) * rng.uniform(8, 15, size=10)
    return X, y, true_coef


def test_huber_loss_piecewise_values():
    delta = 2.0
    assert huber_loss(np.array([0.0]), delta)[0] == 0.0
    np.testing.assert_allclose(huber_loss(np.array([1.0]), delta), [0.5])
    np.testing.assert_allclose(huber_loss(np.array([3.0]), delta),
                               [2.0 * (3.0 - 1.0)])
    # Continuous and C1 at the kink |r| = delta.
    eps = 1e-7
    v_in = huber_loss(np.array([delta - eps]), delta)[0]
    v_out = huber_loss(np.array([delta + eps]), delta)[0]
    assert v_out == pytest.approx(v_in, abs=1e-6)


def test_huber_weights():
    delta = 1.345
    r = np.array([0.0, 0.5, 1.345, 5.0, -5.0])
    w = huber_weight(r, delta)
    np.testing.assert_allclose(w[:3], 1.0)
    assert w[3] == pytest.approx(delta / 5.0)
    assert w[4] == pytest.approx(delta / 5.0)
    assert np.all((w > 0.0) & (w <= 1.0))


def test_huber_recovers_clean_relationship(outlier_data):
    X, y, true_coef = outlier_data
    res = huber_irls(X, y, delta=1.345)
    assert res.converged
    np.testing.assert_allclose(res.coef, true_coef, atol=0.15)
    assert res.intercept == pytest.approx(1.0, abs=0.15)


def test_huber_outperforms_ols_on_outliers(outlier_data):
    X, y, true_coef = outlier_data
    h = huber_irls(X, y)
    o = ols_fit(X, y)
    # Robustness: smaller maximum absolute error against the clean truth.
    err_h = np.max(np.abs(np.r_[h.coef, h.intercept]
                          - np.r_[true_coef, 1.0]))
    err_o = np.max(np.abs(np.r_[o.coef, o.intercept]
                          - np.r_[true_coef, 1.0]))
    assert err_h < err_o
    # Huber must reach a no-worse Huber objective than the OLS point.
    assert h.objective <= o.huber_objective + 1e-8 * max(
        1.0, abs(o.huber_objective))


def test_objective_is_monotone_and_converges(outlier_data):
    X, y, _ = outlier_data
    res = huber_irls(X, y, max_iter=200, tol=1e-10)
    assert res.objective_decreased
    hist = np.array(res.objective_history)
    assert np.all(np.diff(hist) <= 1e-10 * max(1.0, abs(hist[0])))
    assert res.converged
    assert res.iterations <= res.max_iter
    # Final history entry equals reported objective.
    assert res.objective_history[-1] == pytest.approx(res.objective)


def test_max_iter_reached_status(outlier_data):
    X, y, _ = outlier_data
    # A single solve is allowed, and reports one iteration.
    res = huber_irls(X, y, max_iter=1)
    assert res.iterations == 1
    # An impossibly tight tolerance cannot be met in two solves on data
    # with active outliers, so the status must be non-convergence.
    res2 = huber_irls(X, y, max_iter=2, tol=1e-15)
    assert res2.iterations == 2
    assert res2.converged is False
    assert len(res2.objective_history) == 2
    assert res2.objective_history[1] < res2.objective_history[0]


def test_exact_fit_zero_residuals():
    # y = 1 + 2x fits exactly: residuals at machine precision, no NaN.
    X = np.array([[0.0], [1.0], [2.0], [3.0]])
    y = np.array([1.0, 3.0, 5.0, 7.0])
    res = huber_irls(X, y)
    assert res.converged
    assert res.iterations <= 3
    np.testing.assert_allclose(res.coef, [2.0], atol=1e-9)
    assert res.intercept == pytest.approx(1.0, abs=1e-9)
    np.testing.assert_allclose(res.residual, 0.0, atol=1e-12)
    np.testing.assert_allclose(res.weights, 1.0)
    assert res.objective == pytest.approx(0.0, abs=1e-20)
    assert np.all(np.isfinite(res.residual))
    assert np.all(np.isfinite(res.weights))


def test_constant_response_with_intercept():
    X = np.arange(10, dtype=float).reshape(-1, 1)
    y = np.full(10, 4.0)
    res = huber_irls(X, y)
    assert res.converged
    assert res.intercept == pytest.approx(4.0, abs=1e-10)
    assert res.coef[0] == pytest.approx(0.0, abs=1e-10)
    np.testing.assert_allclose(res.weights, 1.0)


def test_intercept_not_penalized():
    # Large ridge penalty must shrink slopes but leave a nonzero intercept.
    rng = np.random.default_rng(7)
    X = rng.normal(size=(80, 1))
    y = 10.0 + 2.0 * X[:, 0] + rng.normal(scale=0.05, size=80)
    res = huber_irls(X, y, lam=1e4, max_iter=100)
    assert abs(res.coef[0]) < 0.5
    assert res.intercept == pytest.approx(10.0, abs=0.5)


def test_no_intercept_fit():
    rng = np.random.default_rng(8)
    X = rng.normal(size=(60, 1))
    y = 3.0 * X[:, 0] + rng.normal(scale=0.05, size=60)
    res = huber_irls(X, y, fit_intercept=False)
    assert res.converged
    assert res.intercept == 0.0
    assert res.coef[0] == pytest.approx(3.0, abs=0.05)


def test_collinear_columns_raise():
    rng = np.random.default_rng(9)
    x = rng.normal(size=40)
    X = np.column_stack([x, -2.5 * x])
    y = x + rng.normal(scale=0.1, size=40)
    with pytest.raises(RankDeficientError) as exc:
        huber_irls(X, y)
    assert exc.value.nullspace_dim == 1


def test_allow_rank_deficient_mode_warns_and_marks():
    rng = np.random.default_rng(10)
    x = rng.normal(size=40)
    X = np.column_stack([x, x])
    y = x + rng.normal(scale=0.1, size=40)
    with pytest.warns(RuntimeWarning):
        res = huber_irls(X, y, allow_rank_deficient=True)
    assert res.rank_deficient
    assert res.nullspace_dim == 1
    assert len(res.warnings) >= 1


def test_irls_weight_does_not_blow_up_with_large_outliers():
    # Very large outliers must produce tiny positive weights, never zero/NaN.
    x_clean = np.linspace(0.0, 1.0, 8)
    X = np.concatenate([x_clean, [0.5, 0.5]]).reshape(-1, 1)
    y = np.concatenate([x_clean, [1e6, -1e6]])
    res = huber_irls(X, y)
    assert res.converged
    assert np.all(res.weights > 0.0)
    assert np.all(np.isfinite(res.weights))
    assert res.weights[-2:].max() < 1e-3


def test_invalid_arguments():
    X = np.ones((4, 1))
    y = np.ones(4)
    with pytest.raises(ValueError):
        huber_irls(X, y, delta=0.0)
    with pytest.raises(ValueError):
        huber_irls(X, y, lam=-1.0)
    with pytest.raises(ValueError):
        huber_irls(X, y, max_iter=0)
    with pytest.raises(ValueError):
        huber_irls(X, y, tol=0.0)


def test_reproducibility(outlier_data):
    X, y, _ = outlier_data
    r1 = huber_irls(X, y, delta=1.345, max_iter=100, tol=1e-11)
    r2 = huber_irls(X, y, delta=1.345, max_iter=100, tol=1e-11)
    # Deterministic algorithm: byte-identical results across calls.
    np.testing.assert_array_equal(r1.coef, r2.coef)
    np.testing.assert_array_equal(r1.intercept, r2.intercept)
    assert r1.objective == r2.objective
    assert r1.iterations == r2.iterations
