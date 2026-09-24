"""角度周期工具测试——周期距离绝不能退化为普通差值。"""

import math

import numpy as np
import pytest

from app.core.angles import (
    angular_distance,
    canonicalize_to_limits,
    nearest_equivalent_in_range,
    wrap_to_pi,
)


def test_wrap_to_pi_basic():
    assert wrap_to_pi(0.0) == 0.0
    assert abs(wrap_to_pi(3.0 * math.pi) - math.pi) < 1e-12
    assert abs(wrap_to_pi(-3.0 * math.pi) - math.pi) < 1e-12
    assert abs(wrap_to_pi(2 * math.pi + 0.1) - 0.1) < 1e-12


def test_angular_distance_is_periodic():
    a = 0.1
    for k in range(-4, 5):
        assert abs(float(angular_distance(a, a + 2 * math.pi * k))) < 1e-12


def test_angular_distance_not_plain_difference():
    # J6 典型场景：5.5 rad 与 -0.78 rad 相差整圈，普通差值 6.28 是错的
    assert abs(float(angular_distance(5.5, -0.7832)) - (5.5 + 0.7832 - 2 * math.pi)) < 1e-9
    assert abs(5.5 - (-0.7832)) > 6.0  # 普通差值确实很大
    assert float(angular_distance(5.5, -0.7832)) < 0.01  # 周期距离很小


def test_angular_distance_arrays_and_range():
    a = np.array([0.0, 1.0, -3.0])
    b = a + 2 * np.pi
    np.testing.assert_allclose(angular_distance(a, b), [0, 0, 0], atol=1e-12)
    d = angular_distance(np.array([0.0, np.pi / 2]), np.array([0.0, -np.pi / 2]))
    np.testing.assert_allclose(d, [0, np.pi])


def test_nearest_equivalent_in_range():
    # J6 ±2π：5.5 与 -0.7832 两个代表都合法
    lo, hi = -2 * np.pi, 2 * np.pi
    assert nearest_equivalent_in_range(5.5, lo, hi) is not None
    assert abs(nearest_equivalent_in_range(7.0, lo, hi) - (7.0 - 2 * np.pi)) < 1e-9
    # 同角的两个 2π 代表“周期距离”恒等，因此用原始数值距离区分：
    # 参考 5.5 选 +5.52，参考 -0.78 选 -0.76（避免无谓整圈回转）
    assert abs(nearest_equivalent_in_range(5.52, lo, hi, reference=5.5) - 5.52) < 1e-9
    assert abs(nearest_equivalent_in_range(5.52, lo, hi, reference=-0.78) + 0.7632) < 1e-3
    # 无参考时选绝对值最小的主值
    assert abs(nearest_equivalent_in_range(7.0, lo, hi) - (7.0 - 2 * np.pi)) < 1e-9
    # 窄区间无等价角
    assert nearest_equivalent_in_range(3.1, -2.967, 2.967) is None


def test_canonicalize_reference_aware():
    low = np.array([-3.0, -2 * np.pi])
    high = np.array([3.0, 2 * np.pi])
    q = np.array([0.5, 5.52])
    ca = canonicalize_to_limits(q, low, high, np.array([0.5, -0.78]))
    cb = canonicalize_to_limits(q, low, high, np.array([0.5, 5.5]))
    assert ca[1] < 0 and cb[1] > 5.0
