"""TimeSeries 插值与时间覆盖约束的验证。"""

import numpy as np
import pytest

from transform_tree import ExtrapolationError, TimeSeries, Transform
from transform_tree.quaternion import from_axis_angle


def _series() -> TimeSeries:
    s = TimeSeries()
    s.insert(0.0, Transform([0, 0, 0], [0, 0, 0, 1]))
    s.insert(2.0, Transform([2, 0, 0], from_axis_angle([0, 0, 1], np.pi / 2)))
    return s


def test_linear_translation_at_midpoint():
    t = _series().interpolate(1.0)
    np.testing.assert_allclose(t.translation, [1, 0, 0], atol=1e-12)


def test_slerp_rotation_at_midpoint():
    t = _series().interpolate(1.0)
    # 0° 与 90° 的中点应为绕 z 轴 45°
    expected = from_axis_angle([0, 0, 1], np.pi / 4)
    np.testing.assert_allclose(t.rotation, expected, atol=1e-12)


def test_exact_sample_time_returns_sample():
    s = _series()
    t = s.interpolate(2.0)
    np.testing.assert_allclose(t.translation, [2, 0, 0], atol=1e-12)


def test_out_of_order_insert_keeps_sorted():
    s = TimeSeries()
    s.insert(2.0, Transform([2, 0, 0], [0, 0, 0, 1]))
    s.insert(0.0, Transform([0, 0, 0], [0, 0, 0, 1]))
    np.testing.assert_allclose(s.interpolate(1.0).translation, [1, 0, 0])


def test_duplicate_time_overwrites():
    s = TimeSeries()
    s.insert(1.0, Transform([1, 0, 0], [0, 0, 0, 1]))
    s.insert(1.0, Transform([5, 0, 0], [0, 0, 0, 1]))
    assert len(s) == 1
    np.testing.assert_allclose(s.interpolate(1.0).translation, [5, 0, 0])


def test_future_time_is_not_filled_with_latest_sample():
    """关键约束：查询晚于最晚样本的时刻必须报错，不得返回最新值。"""
    with pytest.raises(ExtrapolationError):
        _series().interpolate(2.5)


def test_past_time_is_not_filled_with_earliest_sample():
    with pytest.raises(ExtrapolationError):
        _series().interpolate(-0.5)


def test_empty_series_raises():
    with pytest.raises(ExtrapolationError):
        TimeSeries().interpolate(0.0)


def test_single_sample_only_covers_its_instant():
    s = TimeSeries()
    s.insert(1.0, Transform([1, 2, 3], [0, 0, 0, 1]))
    np.testing.assert_allclose(s.interpolate(1.0).translation, [1, 2, 3])
    with pytest.raises(ExtrapolationError):
        s.interpolate(1.1)
