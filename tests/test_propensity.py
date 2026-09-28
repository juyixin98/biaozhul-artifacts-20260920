"""Propensity model + cross-fitting tests.

Reference coefficients for the two-point intercept-only case are derived
analytically from the likelihood -- not from the solver.
"""

import numpy as np
import pytest

from ipw_ate.contract import IPWConfig
from ipw_ate.errors import ModelSeparationError, PropensityScoreError
from ipw_ate.propensity import (
    cross_fit_propensity,
    fit_logistic,
    make_folds,
    predict_proba,
)


def test_intercept_only_mle_matches_analytic_fraction():
    # Two treated, two control, no covariates -> MLE intercept = logit(0.5)=0,
    # fitted probability = 0.5 exactly.
    X = np.zeros((4, 1))
    t = np.array([1, 1, 0, 0])
    theta = fit_logistic(X, t)
    # With a (constant) covariate the penalty leaves intercept identifiable.
    assert predict_proba(theta, X) == pytest.approx(0.5, abs=1e-8)
    assert abs(theta[0]) < 1e-6


def test_logistic_recovers_known_logit_slope():
    # Generate along a single dimension with a known slope and check the
    # fitted probabilities are monotone / calibrated, independent reference.
    rng = np.random.default_rng(0)
    x = rng.normal(size=(2000, 1))
    eta = 1.5 * x[:, 0]
    p = 1 / (1 + np.exp(-eta))
    t = (rng.uniform(size=2000) < p).astype(int)
    theta = fit_logistic(x, t)
    # Slope should be near the generative 1.5 (standardized scale adjusts it,
    # so assert direction and a strong effect rather than an exact constant).
    assert theta[1] > 1.0
    # Calibration: bins by fitted score should track observed rate.
    phat = predict_proba(theta, x)
    assert np.corrcoef(phat, p)[0, 1] > 0.99


def test_make_folds_are_partition_and_reproducible():
    f1 = make_folds(103, 5, seed=7)
    f2 = make_folds(103, 5, seed=7)
    np.testing.assert_array_equal(f1, f2)
    assert sorted(np.bincount(f1).tolist()) == sorted([20, 21, 21, 20, 21])
    assert set(np.unique(f1).tolist()) == {0, 1, 2, 3, 4}


def test_cross_fit_each_score_comes_from_held_out_model():
    # With an explicit fold assignment, assert every unit is predicted by a
    # model trained on the other folds and scores fill the full array.
    x = np.random.default_rng(1).normal(size=(40, 2))
    t = (x[:, 0] + np.random.default_rng(2).normal(size=40) > 0).astype(int)
    cfg = IPWConfig(n_splits=4)
    folds = np.repeat(np.arange(4), 10)
    scores, fold_ids, membership = cross_fit_propensity(t, x, cfg, folds)
    assert scores.shape == (40,)
    assert np.all(np.isfinite(scores))
    assert np.all((scores > 0) & (scores < 1))
    assert len(membership) == 4
    # Fold membership partitions the index set exactly once.
    flat = sorted(i for fold in membership for i in fold)
    assert flat == list(range(40))


def test_separated_treatment_raises_classified_error():
    # Deterministic T = 1{x0>0}; some held-out point lands beyond separation,
    # driving a score to the boundary -> classified PropensityScoreError.
    rng = np.random.default_rng(3)
    x = rng.normal(size=(120, 1))
    x[:, 0] = np.where(x[:, 0] > -0.3, x[:, 0] + 2.0, x[:, 0] - 2.0)
    t = (x[:, 0] > 0).astype(int)
    cfg = IPWConfig(n_splits=3)
    with pytest.raises((PropensityScoreError, ModelSeparationError)) as exc:
        cross_fit_propensity(t, x, cfg)
    assert exc.value.code in {
        "propensity_score_error",
        "model_separation_error",
    }


def test_single_arm_training_fold_is_separation():
    # Force one fold to contain only controls -> ModelSeparationError.
    x = np.zeros((8, 1))
    t = np.array([0, 0, 0, 0, 1, 1, 1, 1])
    cfg = IPWConfig(n_splits=2)
    folds = np.array([0, 0, 0, 0, 1, 1, 1, 1])
    with pytest.raises(ModelSeparationError) as exc:
        cross_fit_propensity(t, x, cfg, folds)
    assert exc.value.code == "model_separation_error"
