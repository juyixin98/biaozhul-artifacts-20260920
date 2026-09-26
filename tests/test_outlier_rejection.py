"""Unit tests for the explicit outlier rejection strategies."""

import numpy as np
import pytest

from icp2d.outlier_rejection import reject_outliers


def test_none_keeps_everything():
    distances = np.array([0.1, 5.0, 100.0])
    result = reject_outliers(distances, strategy="none")
    assert result.inlier_mask.all()
    assert np.isinf(result.threshold_used)


def test_threshold_strategy_uses_gate():
    distances = np.array([0.1, 0.2, 0.49, 0.51, 2.0])
    result = reject_outliers(distances, strategy="threshold", max_distance=0.5)
    np.testing.assert_array_equal(result.inlier_mask, [True, True, True, False, False])


def test_trimmed_strategy_keeps_requested_fraction():
    distances = np.array([1.0, 2.0, 3.0, 4.0, 100.0, 200.0])
    result = reject_outliers(
        distances, strategy="trimmed", trim_ratio=0.5, max_distance=1000.0
    )
    assert result.inlier_mask.sum() == 3
    assert set(np.flatnonzero(result.inlier_mask)) == {0, 1, 2}


def test_trimmed_is_also_bounded_by_max_distance():
    distances = np.array([0.1, 0.2, 60.0, 70.0])
    result = reject_outliers(
        distances, strategy="trimmed", trim_ratio=0.75, max_distance=1.0
    )
    # ceil(0.75 * 4) = 3 would be kept, but the gate drops the two far ones.
    np.testing.assert_array_equal(result.inlier_mask, [True, True, False, False])


def test_mad_strategy_rejects_far_outliers_but_keeps_inliers():
    rng = np.random.default_rng(0)
    inliers = rng.uniform(0.0, 0.05, size=100)
    distances = np.concatenate([inliers, [5.0, 6.0]])
    result = reject_outliers(distances, strategy="mad", max_distance=1.0)
    assert result.inlier_mask[:100].all()
    assert not result.inlier_mask[100:].any()


def test_mad_with_identical_distances_falls_back_to_gate():
    distances = np.full(10, 0.2)
    result = reject_outliers(distances, strategy="mad", max_distance=0.5)
    assert result.inlier_mask.all()


def test_invalid_arguments_raise():
    with pytest.raises(ValueError):
        reject_outliers(np.array([0.1, 0.2]), strategy="bogus")
    with pytest.raises(ValueError):
        reject_outliers(np.array([0.1, 0.2]), strategy="trimmed", trim_ratio=0.0)
    with pytest.raises(ValueError):
        reject_outliers(np.array([0.1, 0.2]), max_distance=-1.0)
