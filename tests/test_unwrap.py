"""计数器回绕修正的单元测试。"""

import numpy as np
import pytest

from odometry.unwrap import unwrap_counter_deltas


def test_no_modulus_plain_diff():
    deltas, wraps = unwrap_counter_deltas(np.array([100, 150, 130]), None)
    assert deltas.tolist() == [0.0, 50.0, -20.0]
    assert not wraps.any()


def test_forward_wrap_corrected():
    # 模 1000：990 → 10 真实增量为 +20
    deltas, wraps = unwrap_counter_deltas(np.array([990, 10]), 1000)
    assert deltas[1] == pytest.approx(20.0)
    assert wraps[1]


def test_backward_wrap_corrected():
    # 模 1000：10 → 990 真实增量为 -20（倒车过零）
    deltas, wraps = unwrap_counter_deltas(np.array([10, 990]), 1000)
    assert deltas[1] == pytest.approx(-20.0)
    assert wraps[1]


def test_no_wrap_no_flag():
    deltas, wraps = unwrap_counter_deltas(np.array([100, 200, 250]), 1000)
    assert deltas.tolist() == [0.0, 100.0, 50.0]
    assert not wraps.any()


def test_multiple_wraps_in_sequence():
    # 每步 +20，连续跨过模 100 的边界两次
    raw = np.array([90, 10, 30, 50, 70, 90, 10])
    deltas, wraps = unwrap_counter_deltas(raw, 100)
    assert deltas[1:] == pytest.approx(np.full(6, 20.0))
    assert wraps[1] and wraps[6]
    assert not wraps[2]


def test_full_range_counter_16bit():
    # 16 位计数器：65535 → 2 真实增量 +3
    deltas, wraps = unwrap_counter_deltas(np.array([65535, 2]), 65536)
    assert deltas[1] == pytest.approx(3.0)
    assert wraps[1]


def test_empty_and_single_sample():
    deltas, wraps = unwrap_counter_deltas(np.array([]), 1000)
    assert deltas.shape == (0,) and wraps.shape == (0,)
    deltas, wraps = unwrap_counter_deltas(np.array([42]), 1000)
    assert deltas.tolist() == [0.0]
    assert not wraps.any()


def test_rejects_2d_input():
    with pytest.raises(ValueError):
        unwrap_counter_deltas(np.zeros((2, 2)), 1000)
