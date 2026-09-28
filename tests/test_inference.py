"""Focused tests for the inference layer: sandwiches, Wald, edge categories."""
from __future__ import annotations

import numpy as np
import pytest

from app.contracts.models import FailureCategory
from app.core.errors import EstimationError
from app.core.inference import coefficient_inference, wald_test, weighted_ols_cluster


def _design(n_t=10, n_c=10, treat_effect=2.0, seed=0):
    rng = np.random.default_rng(seed)
    n = n_t + n_c
    t = np.r_[np.ones(n_t), np.zeros(n_c)]
    y = treat_effect * t + rng.normal(0, 1, n)
    x = np.column_stack([np.ones(n), t])
    w = np.ones(n)
    clusters = np.array([f"u{i}" for i in range(n)])
    return x, y, w, clusters


def test_recovers_treatment_effect_and_ci():
    x, y, w, clusters = _design(treat_effect=2.0, seed=7)
    res = weighted_ols_cluster(x, y, w, clusters, coef_names=["a", "b"])
    inf = coefficient_inference(res, 1, alpha=0.05)
    assert abs(inf["value"] - 2.0) < 0.6
    assert inf["ci_low"] < inf["value"] < inf["ci_high"]
    assert inf["dof"] == res.n_clusters - 1


def test_wald_rejects_zero_effect_does_not_reject_null():
    x, y, w, clusters = _design(treat_effect=3.0, seed=3)
    res = weighted_ols_cluster(x, y, w, clusters)
    f_big, p_big = wald_test(res, [1])
    assert p_big < 0.05
    # Joint test of intercept=grand-mean-ish when no effect: build null data.
    x0, y0, w0, c0 = _design(treat_effect=0.0, seed=11)
    res0 = weighted_ols_cluster(x0, y0, w0, c0)
    _, p_null = wald_test(res0, [1])
    assert p_null > 0.05


def test_singular_design_is_category():
    # Second column is a duplicate of the intercept.
    x = np.column_stack([np.ones(6), np.ones(6)])
    y = np.arange(6, dtype=float)
    with pytest.raises(EstimationError) as exc:
        weighted_ols_cluster(x, y, np.ones(6), np.array([f"u{i}" for i in range(6)]),
                             coef_names=["a", "dup"])
    assert exc.value.category is FailureCategory.SINGULAR_DESIGN


def test_insufficient_clusters_category():
    x = np.column_stack([np.ones(4), np.array([0, 0, 1, 1])])
    y = np.array([1.0, 2.0, 3.0, 5.0])
    with pytest.raises(EstimationError) as exc:
        weighted_ols_cluster(x, y, np.ones(4), np.array(["only"] * 4))
    assert exc.value.category is FailureCategory.INSUFFICIENT_CLUSTERS


def test_saturated_design_no_residual_dof():
    # n = p = 2: point estimate exact, variance undefined.
    x = np.column_stack([np.ones(2), np.array([0.0, 1.0])])
    y = np.array([1.0, 4.0])
    with pytest.raises(EstimationError) as exc:
        weighted_ols_cluster(x, y, np.ones(2), np.array(["u0", "u1"]))
    assert exc.value.category is FailureCategory.NO_RESIDUAL_DEGREES


def test_invalid_weight_and_nonfinite_are_categories():
    x = np.column_stack([np.ones(4), np.array([0, 0, 1, 1.0])])
    y = np.array([1.0, 2.0, 3.0, 4.0])
    with pytest.raises(EstimationError) as exc:
        weighted_ols_cluster(x, y, np.array([1, -1, 1, 1.0]), np.array(list("abcd")))
    assert exc.value.category is FailureCategory.INVALID_WEIGHT
    y_bad = np.array([1.0, np.nan, 3.0, 4.0])
    with pytest.raises(EstimationError) as exc:
        weighted_ols_cluster(x, y_bad, np.ones(4), np.array(list("abcd")))
    assert exc.value.category is FailureCategory.NON_FINITE_OUTCOME


def test_empty_rows_category():
    with pytest.raises(EstimationError) as exc:
        weighted_ols_cluster(np.empty((0, 2)), np.empty(0), np.empty(0), np.empty(0))
    assert exc.value.category is FailureCategory.NO_ESTIMABLE_UNITS


def test_crv0_adjustment_runs_and_differs_from_crv1():
    x, y, w, clusters = _design(seed=5)
    r1 = weighted_ols_cluster(x, y, w, clusters, cluster_adjustment="crv1")
    r0 = weighted_ols_cluster(x, y, w, clusters, cluster_adjustment="cr0")
    # CRV1 multiplies by G/(G-1)*(n-1)/(n-p) > 1, so its SE is larger.
    assert r1.se[1] > r0.se[1]
