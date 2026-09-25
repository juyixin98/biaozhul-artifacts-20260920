"""Tests for missing-value strategies."""

import numpy as np
import pytest

from abtest.missing import MissingStrategy, apply_missing_strategy, count_missing


def test_drop_removes_nans():
    arr = np.array([1.0, np.nan, 3.0, np.nan, 5.0])
    out = apply_missing_strategy(arr, MissingStrategy.DROP)
    np.testing.assert_array_equal(out, [1.0, 3.0, 5.0])


def test_impute_mean_fills_with_observed_mean():
    arr = np.array([1.0, np.nan, 3.0])
    out = apply_missing_strategy(arr, MissingStrategy.IMPUTE_MEAN)
    np.testing.assert_allclose(out, [1.0, 2.0, 3.0])


def test_impute_mean_preserves_length():
    arr = np.array([np.nan, 2.0, np.nan, 4.0])
    out = apply_missing_strategy(arr, MissingStrategy.IMPUTE_MEAN)
    assert out.size == 4
    assert not np.isnan(out).any()


def test_all_missing_raises():
    arr = np.array([np.nan, np.nan])
    with pytest.raises(ValueError, match="no valid"):
        apply_missing_strategy(arr, MissingStrategy.DROP)
    with pytest.raises(ValueError, match="no valid"):
        apply_missing_strategy(arr, MissingStrategy.IMPUTE_MEAN)


def test_no_missing_is_identity():
    arr = np.array([1.0, 2.0, 3.0])
    for strategy in MissingStrategy:
        np.testing.assert_array_equal(apply_missing_strategy(arr, strategy), arr)


def test_count_missing():
    assert count_missing([1.0, None, float("nan"), 2.0]) == 2
    assert count_missing([1.0, 2.0]) == 0
