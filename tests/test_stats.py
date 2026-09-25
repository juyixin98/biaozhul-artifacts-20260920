"""Tests for the Welch mean-difference confidence interval."""

import numpy as np
import pytest

from abtest.stats import welch_degrees_of_freedom, welch_mean_diff_ci


def test_known_value_matches_hand_computation():
    # Hand-computed Welch 95% CI for two small samples.
    control = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    treatment = np.array([2.0, 3.0, 4.0, 5.0, 6.0])
    res = welch_mean_diff_ci(control, treatment, confidence_level=0.95)
    # Means differ by exactly 1; equal sizes and equal sample variances
    # (var = 2.5 each), so SE = sqrt(2.5/5 + 2.5/5) = 1.0, df = 8,
    # t_{0.975, 8} = 2.3060 -> CI = 1 +/- 2.3060.
    assert res.mean_diff == pytest.approx(1.0)
    assert res.std_error == pytest.approx(1.0)
    assert res.degrees_of_freedom == pytest.approx(8.0)
    assert res.ci_lower == pytest.approx(1.0 - 2.3060, abs=1e-3)
    assert res.ci_upper == pytest.approx(1.0 + 2.3060, abs=1e-3)


def test_identical_constant_groups_give_zero_width_interval():
    res = welch_mean_diff_ci([2.0, 2.0, 2.0], [2.0, 2.0, 2.0])
    assert res.mean_diff == 0.0
    assert res.ci_lower == pytest.approx(0.0)
    assert res.ci_upper == pytest.approx(0.0)


def test_missing_drop_reduces_n():
    res = welch_mean_diff_ci(
        [1.0, 2.0, float("nan"), 4.0],
        [1.0, 2.0, 3.0, 4.0],
        missing_strategy="drop",
    )
    assert res.n_control == 3
    assert res.n_treatment == 4
    assert res.n_missing_control == 1
    assert res.n_missing_treatment == 0


def test_missing_impute_mean_keeps_n():
    res = welch_mean_diff_ci(
        [1.0, 2.0, float("nan"), 4.0],
        [1.0, 2.0, 3.0, 4.0],
        missing_strategy="impute_mean",
    )
    assert res.n_control == 4
    assert res.n_missing_control == 1


def test_imbalanced_groups_supported():
    rng = np.random.default_rng(7)
    control = rng.standard_normal(30)
    treatment = rng.standard_normal(3000)
    res = welch_mean_diff_ci(control, treatment)
    assert res.n_control == 30
    assert res.n_treatment == 3000
    # SE is dominated by the small group.
    assert res.std_error == pytest.approx(
        np.sqrt(
            np.var(control, ddof=1) / 30 + np.var(treatment, ddof=1) / 3000
        )
    )


def test_higher_confidence_gives_wider_interval():
    rng = np.random.default_rng(11)
    c = rng.standard_normal(100)
    t = rng.standard_normal(100)
    r90 = welch_mean_diff_ci(c, t, confidence_level=0.90)
    r99 = welch_mean_diff_ci(c, t, confidence_level=0.99)
    w90 = r90.ci_upper - r90.ci_lower
    w99 = r99.ci_upper - r99.ci_lower
    assert w99 > w90


def test_too_few_observations_raise():
    with pytest.raises(ValueError, match="at least"):
        welch_mean_diff_ci([1.0], [1.0, 2.0])
    with pytest.raises(ValueError, match="at least"):
        welch_mean_diff_ci([1.0, float("nan")], [1.0, 2.0])


def test_invalid_confidence_level_raises():
    with pytest.raises(ValueError):
        welch_mean_diff_ci([1.0, 2.0], [1.0, 2.0], confidence_level=1.5)


def test_unknown_missing_strategy_raises():
    with pytest.raises(ValueError):
        welch_mean_diff_ci([1.0, 2.0], [1.0, 2.0], missing_strategy="zero")


def test_result_serializes_to_json_dict():
    import json

    res = welch_mean_diff_ci([1.0, 2.0, 3.0], [2.0, 3.0, 4.0])
    json.dumps(res.to_dict())  # must not raise


def test_welch_df_bounds():
    # df lies between min(n_c, n_t) - 1 and n_c + n_t - 2.
    rng = np.random.default_rng(3)
    c = rng.standard_normal(25)
    t = rng.standard_normal(400)
    df = welch_degrees_of_freedom(
        float(np.var(c, ddof=1)), 25, float(np.var(t, ddof=1)), 400
    )
    assert 24 <= df <= 423
