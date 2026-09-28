"""高精度参考模块测试：与 Fraction 精确值及已知常数交叉验证。

参考实现本身也需要被验证 —— 否则"用被测核心自证"会形成循环。
"""

from __future__ import annotations

import random

import mpmath

from app.reference import high_precision_sum, high_precision_sum_abs

from conftest import exact_sum, exact_sum_abs


def test_reference_matches_fraction_on_random_values() -> None:
    rng = random.Random(123)
    values = [rng.uniform(-1e8, 1e8) for _ in range(500)]
    ref = high_precision_sum(values, dps=60)
    exact = exact_sum(values)
    # 60 位十进制下两者应一致到 1e-40 相对量级
    assert abs(float(ref) - float(exact)) <= 1e-8 * max(abs(float(exact)), 1.0)
    assert mpmath.mpf(float(exact)) == mpmath.mpf(float(ref))


def test_reference_sum_abs_matches_fraction() -> None:
    rng = random.Random(321)
    values = [rng.uniform(-1e4, 1e4) for _ in range(300)]
    assert float(high_precision_sum_abs(values)) == float(exact_sum_abs(values))


def test_reference_recovers_known_constant() -> None:
    # 巴塞尔问题部分和：sum 1/k^2 -> pi^2/6；与实现无关的已知值
    n = 2000
    values = [1.0 / (k * k) for k in range(1, n + 1)]
    ref = high_precision_sum(values, dps=60)
    tail_bound = 1.0 / n  # 积分判别法上界
    assert abs(float(ref) - float(mpmath.pi**2 / 6)) < tail_bound


def test_reference_handles_signed_zero_exactly() -> None:
    assert high_precision_sum([-0.0, -0.0]) == 0
