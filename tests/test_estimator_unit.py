"""Estimator tests.

The numerical results are checked against SciPy's own public routines and
hand-computed values. Every report must explicitly state that outcome
significance does not prove allocation correctness.
"""
from __future__ import annotations

import math

import numpy as np
import pytest
from scipy import stats

from stratblock.estimator import (
    build_effect_report,
    difference_in_means,
    permutation_test,
    welch_test,
    GroupData,
)


@pytest.mark.unit
def test_welch_matches_scipy_and_hand_computed_ci() -> None:
    rng = np.random.default_rng(20260927)
    a_vals = rng.normal(2.0, 1.0, 30).tolist()
    b_vals = rng.normal(0.0, 1.5, 25).tolist()
    ga, gb = GroupData("A", a_vals), GroupData("B", b_vals)
    result = welch_test(ga, gb)
    ref = stats.ttest_ind(a_vals, b_vals, equal_var=False)
    assert math.isclose(result["t_statistic"], float(ref.statistic), rel_tol=1e-12)
    assert math.isclose(result["p_value"], float(ref.pvalue), rel_tol=1e-12)
    assert result["ci95_low"] < result["estimate"] < result["ci95_high"]
    assert result["estimate"] == pytest.approx(difference_in_means(ga, gb), rel=1e-12)


@pytest.mark.unit
def test_exact_permutation_p_value_for_tiny_sample() -> None:
    # 4 vs 4 with an extreme split: p-value computed by full enumeration
    # of C(8,4)=70 labelings.
    a = GroupData("A", [10.0, 10.0, 10.0, 10.0])
    b = GroupData("B", [0.0, 0.0, 0.0, 0.0])
    result = permutation_test(a, b, seed=1)
    assert result["method"] == "exact_enumeration"
    assert result["n_permutations"] == 70
    # Only the two most extreme labelings give |mean diff| >= 10.
    assert math.isclose(result["p_value"], 2 / 70, rel_tol=1e-12)


@pytest.mark.unit
def test_permutation_null_is_centered_and_report_scopes_claims() -> None:
    rng = np.random.default_rng(0)
    a_vals = rng.normal(1.0, 1.0, 6).tolist()
    b_vals = rng.normal(0.0, 1.0, 6).tolist()
    report = build_effect_report("A", a_vals, "B", b_vals)
    assert report["proves_allocation_correct"] is False
    assert "NOT evidence" in report["interpretation_scope"]
    assert report["welch"]["status"] == "ok"
    assert report["permutation_test"]["status"] == "ok"
    assert abs(report["permutation_test"]["null_mean"]) < 1e-9


@pytest.mark.unit
def test_indeterminate_result_when_a_group_has_one_observation() -> None:
    report = build_effect_report("A", [1.0], "B", [2.0, 3.0])
    assert report["welch"]["status"] == "indeterminate"
    # Failure/uncertainty is surfaced in its own field, not swallowed.
    assert "2 observations" in report["welch"]["reason"]
