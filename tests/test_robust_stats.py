"""StreamingRobustStats 单元测试。"""

import math

import numpy as np
import pytest

from burst_detector.robust_stats import MAD_TO_STD, StreamingRobustStats


def test_empty_window_stats_are_nan():
    s = StreamingRobustStats(5)
    assert s.size == 0
    assert math.isnan(s.median())
    assert math.isnan(s.mad())
    assert math.isnan(s.scale())


def test_fifo_ordering_with_wrap_around():
    s = StreamingRobustStats(3)
    for v in [1.0, 2.0, 3.0, 4.0]:
        s.add(v)
    # 最旧的 1.0 被淘汰，窗口按时间顺序为 [2, 3, 4]。
    np.testing.assert_array_equal(s.values(), [2.0, 3.0, 4.0])
    assert s.median() == 3.0


def test_median_and_mad_robust_to_outlier():
    s = StreamingRobustStats(5)
    for v in [1.0, 1.0, 1.0, 1.0, 100.0]:
        s.add(v)
    # 中位数对单个极端值稳健；MAD=0（半数以上相同）。
    assert s.median() == 1.0
    assert s.mad() == 0.0
    assert s.scale(min_scale=1e-6) == 1e-6


def test_scale_matches_gaussian_sigma():
    rng = np.random.default_rng(42)
    x = rng.standard_normal(10000) * 3.0
    s = StreamingRobustStats(10000)
    for v in x:
        s.add(v)
    assert s.scale() == pytest.approx(3.0, rel=0.05)
    assert MAD_TO_STD == pytest.approx(1.0 / 0.6744897501960817)


def test_non_finite_values_rejected():
    s = StreamingRobustStats(3)
    with pytest.raises(ValueError):
        s.add(float("nan"))
    with pytest.raises(ValueError):
        s.add(float("inf"))


def test_invalid_window_size():
    with pytest.raises(ValueError):
        StreamingRobustStats(0)
