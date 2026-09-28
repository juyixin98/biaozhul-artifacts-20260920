"""Tests for the ridge-logistic propensity model.

Recovery and calibration are checked against independently generated data
using NumPy primitives in the test itself (no reuse of the fitting code to
build the reference answer).
"""

from __future__ import annotations

import numpy as np
import pytest

from ipwate.errors import ModelConvergenceError, ValidationError
from ipwate.model import fit_logistic_ridge


def _generate_logit(n=4000, seed=0, beta=(0.3, 1.1, -0.7)):
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(n, 2))
    eta = beta[0] + beta[1] * x[:, 0] + beta[2] * x[:, 1]
    p = 1.0 / (1.0 + np.exp(-eta))
    a = (rng.uniform(size=n) < p).astype(np.int8)
    return x, a, beta


def test_recovers_known_logistic_coefficients():
    x, a, beta = _generate_logit()
    model = fit_logistic_ridge(
        x, a, ridge_lambda=1e-6, max_iter=100, tol=1e-10
    )
    # Slopes are on the STANDARDIZED scale; standardize X independently here.
    xs = (x - x.mean(axis=0)) / x.std(axis=0, ddof=0)
    expected_slopes = np.array([beta[1] * x.std(axis=0, ddof=0)[0],
                                beta[2] * x.std(axis=0, ddof=0)[1]])
    np.testing.assert_allclose(model.beta[1:], expected_slopes, atol=0.15)
    # Intercept on standardized design: b0_std = b0 + sum(b_j * mean_j / sd_j)
    expected_intercept = beta[0] + np.sum(
        np.array(beta[1:]) * x.mean(axis=0) / x.std(axis=0, ddof=0)
    )
    assert model.beta[0] == pytest.approx(expected_intercept, abs=0.1)
    assert model.converged


def test_predictions_are_valid_probabilities_and_oof_accurate():
    x, a, _ = _generate_logit(seed=5)
    cut = 3000
    model = fit_logistic_ridge(
        x[:cut], a[:cut], ridge_lambda=1e-3, max_iter=100, tol=1e-8
    )
    p = model.predict_proba(x[cut:])
    assert np.all((p > 0) & (p < 1))
    # Independently computed log-loss sanity bound.
    eps = 1e-12
    logloss = -np.mean(
        a[cut:] * np.log(np.clip(p, eps, 1)) +
        (1 - a[cut:]) * np.log(np.clip(1 - p, eps, 1))
    )
    assert logloss < 0.62


def test_separation_is_reported_not_silently_clipped():
    # Hard separation by X1: unpenalized logistic cannot converge to a finite beta.
    rng = np.random.default_rng(2)
    x = rng.normal(size=(400, 1))
    a = (x[:, 0] > 0).astype(np.int8)
    with pytest.raises(ModelConvergenceError) as exc:
        fit_logistic_ridge(x, a, ridge_lambda=0.0, max_iter=50, tol=1e-8)
    assert exc.value.code == "model_did_not_converge"


def test_single_class_training_fold_rejected():
    x = np.zeros((10, 2))
    a = np.ones(10, dtype=np.int8)
    with pytest.raises(ValidationError) as exc:
        fit_logistic_ridge(x, a, ridge_lambda=0.1, max_iter=10, tol=1e-8)
    assert exc.value.code == "validation_error"
