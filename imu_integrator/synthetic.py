"""合成 IMU 数据生成器（无硬件、无外部数据源）。

三个核验场景：

1. :func:`stationary_samples` —— 静止水平放置，加计量得反重力；
2. :func:`constant_rotation_samples` —— 绕机体系定轴匀速转动；
3. :func:`constant_acceleration_samples` —— 姿态不变、世界系恒线加速度。

生成的信号在每个采样区间上为**左端点常值**，与积分器的分段常值
假设严格一致，因此可用解析真值核验。时间间隔支持非均匀（变 dt）。
所有生成器都可叠加固定的陀螺/加计偏置，交由积分器扣除。
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

from .quaternion import quat_from_angle_axis, quat_rotate, quat_conjugate


def _timestamps(duration: float, dt: float | Sequence[float], start_time: float = 0.0) -> np.ndarray:
    if np.isscalar(dt):
        n = int(round(duration / float(dt)))
        if n < 1:
            raise ValueError("duration 至少要包含一个采样间隔")
        intervals = np.full(n, float(dt))
    else:
        intervals = np.asarray(dt, dtype=float)
        if intervals.ndim != 1 or len(intervals) < 1:
            raise ValueError("dt 数组必须是非空一维序列")
        if np.any(intervals <= 0):
            raise ValueError("所有时间间隔必须为正")
        if duration is not None and not np.isclose(intervals.sum(), float(duration), rtol=1e-10):
            raise ValueError("dt 数组之和必须等于 duration")
    return np.concatenate(([float(start_time)], start_time + np.cumsum(intervals)))


def _records(t: np.ndarray, gyro: np.ndarray, accel: np.ndarray) -> list[dict]:
    return [
        {
            "t": float(t[k]),
            "gyro": gyro[k].tolist(),
            "accel": accel[k].tolist(),
        }
        for k in range(len(t))
    ]


def stationary_samples(
    duration: float = 1.0,
    dt: float | Sequence[float] = 0.01,
    *,
    gravity: Sequence[float] = (0.0, 0.0, -9.81),
    gyro_bias: Sequence[float] = (0.0, 0.0, 0.0),
    accel_bias: Sequence[float] = (0.0, 0.0, 0.0),
    start_time: float = 0.0,
) -> list[dict]:
    """静止水平放置：陀螺输出偏置，加计输出 -g + 偏置（水平时为 +9.81 z）。"""
    t = _timestamps(duration, dt, start_time)
    g = np.asarray(gravity, dtype=float)
    bg = np.asarray(gyro_bias, dtype=float)
    ba = np.asarray(accel_bias, dtype=float)
    gyro = np.tile(bg, (len(t), 1))
    accel = np.tile(-g + ba, (len(t), 1))
    return _records(t, gyro, accel)


def constant_rotation_samples(
    angular_velocity: Sequence[float] = (0.0, 0.0, 1.0),
    duration: float = 1.0,
    dt: float | Sequence[float] = 0.01,
    *,
    gravity: Sequence[float] = (0.0, 0.0, -9.81),
    gyro_bias: Sequence[float] = (0.0, 0.0, 0.0),
    accel_bias: Sequence[float] = (0.0, 0.0, 0.0),
    initial_orientation: Sequence[float] = (1.0, 0.0, 0.0, 0.0),
    start_time: float = 0.0,
) -> list[dict]:
    """绕机体系固定轴以恒定角速率转动（原点不动，加计只感受重力）。

    q(t) = q0 ⊗ exp(omega t /2)；加速度计读数为 R(t)^T·(-g) + 偏置。
    """
    t = _timestamps(duration, dt, start_time)
    omega = np.asarray(angular_velocity, dtype=float)
    g = np.asarray(gravity, dtype=float)
    bg = np.asarray(gyro_bias, dtype=float)
    ba = np.asarray(accel_bias, dtype=float)
    q0 = np.asarray(initial_orientation, dtype=float)
    rate = float(np.linalg.norm(omega))

    gyro = np.empty((len(t), 3))
    accel = np.empty((len(t), 3))
    for k, tk in enumerate(t):
        if rate < 1e-15:
            q = q0
        else:
            q = quat_from_angle_axis(rate * (tk - t[0]), omega / rate)
            q = _quat_compose(q0, q)
        gyro[k] = omega + bg
        # 机体系比力 = R^T (a_point - g)，原点不动 a_point = 0
        accel[k] = quat_rotate(quat_conjugate(q), -g) + ba
    return _records(t, gyro, accel)


def constant_acceleration_samples(
    acceleration_world: Sequence[float] = (1.0, 0.0, 0.0),
    duration: float = 1.0,
    dt: float | Sequence[float] = 0.01,
    *,
    gravity: Sequence[float] = (0.0, 0.0, -9.81),
    gyro_bias: Sequence[float] = (0.0, 0.0, 0.0),
    accel_bias: Sequence[float] = (0.0, 0.0, 0.0),
    initial_orientation: Sequence[float] = (1.0, 0.0, 0.0, 0.0),
    start_time: float = 0.0,
) -> list[dict]:
    """姿态恒定、世界系内恒定线加速度 a：加计读数 R^T(a - g) + 偏置。"""
    t = _timestamps(duration, dt, start_time)
    a = np.asarray(acceleration_world, dtype=float)
    g = np.asarray(gravity, dtype=float)
    bg = np.asarray(gyro_bias, dtype=float)
    ba = np.asarray(accel_bias, dtype=float)
    q0 = np.asarray(initial_orientation, dtype=float)

    gyro = np.tile(bg, (len(t), 1))
    f_body = quat_rotate(quat_conjugate(q0), a - g) + ba
    accel = np.tile(f_body, (len(t), 1))
    return _records(t, gyro, accel)


def _quat_compose(q0: np.ndarray, dq: np.ndarray) -> np.ndarray:
    from .quaternion import quat_multiply

    return quat_multiply(q0, dq)
