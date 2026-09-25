"""Tests for the individual transformers."""
import numpy as np
import pytest

from feature_pipeline import OneHotEncoder, SimpleImputer, StandardScaler
from feature_pipeline.exceptions import FittingError, NotFittedError


# --------------------------------------------------------------------------- #
# SimpleImputer
# --------------------------------------------------------------------------- #
def test_numeric_mean_imputation_ignores_nan_and_is_fitted_state():
    imp = SimpleImputer(strategy="mean")
    column = np.array([1.0, np.nan, 3.0, np.nan, 5.0])

    out = imp.fit_transform(column)

    np.testing.assert_allclose(imp.fill_value_, 3.0)
    np.testing.assert_allclose(out, [1.0, 3.0, 3.0, 3.0, 5.0])


def test_numeric_median_and_constant_strategies():
    column = np.array([1.0, 2.0, np.nan, 100.0])

    median = SimpleImputer(strategy="median").fit(column)
    np.testing.assert_allclose(median.fill_value_, 2.0)

    const = SimpleImputer(strategy="constant", fill_value=-9.0).fit(column)
    np.testing.assert_allclose(const.transform(column), [1.0, 2.0, -9.0, 100.0])


def test_mean_imputer_fails_on_all_missing_numeric_column():
    imp = SimpleImputer(strategy="mean")
    with pytest.raises(FittingError):
        imp.fit(np.array([np.nan, np.nan]))


def test_categorical_most_frequent_is_deterministic_on_tie():
    imp = SimpleImputer(strategy="most_frequent")
    # a and b tie -> lexicographically smallest must win deterministically
    out = imp.fit_transform(np.array(["a", None, "b", None], dtype=object))
    assert out.tolist() == ["a", "a", "b", "a"]


def test_categorical_constant_imputation():
    imp = SimpleImputer(strategy="constant", fill_value="missing")
    out = imp.fit_transform(np.array(["x", None, "y"], dtype=object))
    assert out.tolist() == ["x", "missing", "y"]


def test_imputer_transform_before_fit_raises():
    with pytest.raises(NotFittedError):
        SimpleImputer(strategy="mean").transform(np.array([1.0]))


# --------------------------------------------------------------------------- #
# StandardScaler
# --------------------------------------------------------------------------- #
def test_standard_scaler_uses_fitted_mean_and_std():
    scaler = StandardScaler()
    train = np.array([0.0, 0.0, 10.0, 10.0])
    scaler.fit(train)

    np.testing.assert_allclose(scaler.mean_, 5.0)
    np.testing.assert_allclose(scaler.scale_, 5.0)
    # An unseen test value is scaled with TRAIN statistics only.
    np.testing.assert_allclose(scaler.transform(np.array([15.0])), [2.0])


def test_standard_scaler_zero_variance_emits_zeros_without_divide_error():
    scaler = StandardScaler()
    train = np.array([7.0, 7.0, 7.0])

    out = scaler.fit_transform(train)

    assert scaler.zero_variance_ is True
    assert scaler.scale_ == 1.0  # safe fallback, never divide by zero
    np.testing.assert_allclose(out, [0.0, 0.0, 0.0])


def test_scaler_transform_before_fit_raises():
    with pytest.raises(NotFittedError):
        StandardScaler().transform(np.array([1.0]))


# --------------------------------------------------------------------------- #
# OneHotEncoder
# --------------------------------------------------------------------------- #
def test_one_hot_encoder_categories_are_sorted_and_stable():
    enc = OneHotEncoder()  # default handle_unknown="indicator" -> trailing flag col
    out, unknown = enc.fit_transform(np.array(["b", "a", "c", "a"], dtype=object))

    assert enc.categories_ == ["a", "b", "c"]
    expected = np.array(
        [[0.0, 1.0, 0.0, 0.0],
         [1.0, 0.0, 0.0, 0.0],
         [0.0, 0.0, 1.0, 0.0],
         [1.0, 0.0, 0.0, 0.0]]
    )
    np.testing.assert_allclose(out, expected)
    assert unknown.tolist() == [False, False, False, False]


def test_one_hot_unknown_category_is_all_zeros_and_flagged():
    enc = OneHotEncoder(handle_unknown="ignore")
    enc.fit(np.array(["a", "b"], dtype=object))

    out, unknown = enc.transform(np.array(["a", "zzz", "b"], dtype=object))
    np.testing.assert_allclose(out[1], [0.0, 0.0])
    assert unknown.tolist() == [False, True, False]


def test_one_hot_unknown_indicator_strategy_adds_flag_column():
    enc = OneHotEncoder(handle_unknown="indicator")
    enc.fit(np.array(["a", "b"], dtype=object))

    out, unknown = enc.transform(np.array(["a", "zzz"], dtype=object))
    assert out.shape == (2, 3)
    np.testing.assert_allclose(out[:, 2], [0.0, 1.0])
    assert unknown.tolist() == [False, True]
