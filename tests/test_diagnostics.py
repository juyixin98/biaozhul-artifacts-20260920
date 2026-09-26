"""诊断模块单元测试。"""

import numpy as np

from odometry.config import DiagnosticThresholds
from odometry.diagnostics import (
    FLAG_ANGULAR_SPIKE,
    FLAG_NON_MONOTONIC_TIME,
    FLAG_TIME_GAP,
    FLAG_VELOCITY_SPIKE,
    FLAG_WRAP_LEFT,
    FLAG_WRAP_RIGHT,
    diagnose_steps,
)

THRESHOLDS = DiagnosticThresholds(
    max_linear_velocity=2.0, max_angular_velocity=5.0, max_dt=0.5
)


def _run(timestamps, ds_center, dtheta, wrap_left=None, wrap_right=None):
    n = len(timestamps)
    return diagnose_steps(
        np.array(timestamps, dtype=float),
        np.array(ds_center, dtype=float),
        np.array(dtheta, dtype=float),
        np.zeros(n, dtype=bool) if wrap_left is None else np.array(wrap_left),
        np.zeros(n, dtype=bool) if wrap_right is None else np.array(wrap_right),
        THRESHOLDS,
    )


def test_clean_sequence_no_flags():
    flags = _run([0.0, 0.1, 0.2], [0.0, 0.01, 0.01], [0.0, 0.0, 0.0])
    assert flags == [[], [], []]


def test_time_gap_flagged():
    # 0.2 → 1.0 间隔 0.8 s > max_dt 0.5 → 丢样标记
    flags = _run([0.0, 0.2, 1.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0])
    assert FLAG_TIME_GAP in flags[2]
    assert flags[0] == [] and flags[1] == []


def test_non_monotonic_time_flagged():
    flags = _run([0.0, 0.2, 0.1], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0])
    assert FLAG_NON_MONOTONIC_TIME in flags[2]


def test_velocity_spike_flagged():
    # dt=0.1 s 内位移 1.0 m → 10 m/s > 2 m/s
    flags = _run([0.0, 0.1], [0.0, 1.0], [0.0, 0.0])
    assert FLAG_VELOCITY_SPIKE in flags[1]


def test_angular_spike_flagged():
    # dt=0.1 s 内转向 1.0 rad → 10 rad/s > 5 rad/s
    flags = _run([0.0, 0.1], [0.0, 0.0], [0.0, 1.0])
    assert FLAG_ANGULAR_SPIKE in flags[1]


def test_wrap_flags_propagated():
    flags = _run(
        [0.0, 0.1, 0.2],
        [0.0, 0.0, 0.0],
        [0.0, 0.0, 0.0],
        wrap_left=[False, True, False],
        wrap_right=[False, False, True],
    )
    assert FLAG_WRAP_LEFT in flags[1]
    assert FLAG_WRAP_RIGHT in flags[2]
