"""鲁棒核权重与代价的性质。"""

from __future__ import annotations

import numpy as np
import pytest

from pose_graph_optimizer.kernels import Kernel, kernel_weight, robust_cost


def test_invalid_kernel_rejected():
    with pytest.raises(ValueError):
        Kernel("l0", 1.0)
    with pytest.raises(ValueError):
        Kernel("huber", 0.0)
    with pytest.raises(ValueError):
        Kernel("huber", -1.0)


def test_none_kernel_is_ordinary_least_squares():
    k = Kernel("none")
    for u in (0.0, 0.5, 10.0, 1000.0):
        assert kernel_weight(k, u) == 1.0
        assert robust_cost(k, u) == pytest.approx(0.5 * u)


def test_huber_piecewise_weight():
    k = Kernel("huber", 1.0)
    assert kernel_weight(k, 0.25) == 1.0          # r=0.5 <= delta
    assert kernel_weight(k, 4.0) == pytest.approx(0.5)  # r=2 -> 1/2
    # 大残差仍有非零权重（软抑制，不会完全拒绝）
    assert kernel_weight(k, 10000.0) == pytest.approx(0.01)
    assert kernel_weight(k, 10000.0) > 0.0


def test_cauchy_weight_monotonic_but_never_zero():
    k = Kernel("cauchy", 1.0)
    small = kernel_weight(k, 0.01)
    large = kernel_weight(k, 10000.0)
    assert 0.0 < large < small < 1.0


def test_tukey_hard_rejection():
    k = Kernel("tukey", 1.0)
    assert kernel_weight(k, 0.0) == 1.0
    # r = 0.5：权重 (1 - 0.25)^2 = 0.5625
    assert kernel_weight(k, 0.25) == pytest.approx(0.5625)
    # 超过阈值直接归零（硬拒绝错误回环）
    assert kernel_weight(k, 1.0) == 0.0
    assert kernel_weight(k, 10000.0) == 0.0


def test_cost_monotone_and_continuous_at_threshold():
    for name in ("huber", "tukey"):
        k = Kernel(name, 1.0)
        u_in = 0.99**2
        u_out = 1.01**2
        assert robust_cost(k, u_out) >= robust_cost(k, u_in) - 1e-12
        # 阈值处代价连续
        assert robust_cost(k, 1.0 + 1e-9) == pytest.approx(
            robust_cost(k, 1.0 - 1e-9), abs=1e-6
        )
    cauchy = Kernel("cauchy", 2.0)
    assert robust_cost(cauchy, 100.0) < 0.5 * 100.0  # 有界抑制
