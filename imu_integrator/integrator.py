"""离线 IMU 积分（已知重力、固定偏置）。

模型（无地球自转/运输率补偿，适用于小型离线实验的局部平面世界系）::

    w_k = gyro_k - b_g                    # 机体系角速率（rad/s）
    f_k = accel_k - b_a                   # 机体系比力（m/s^2）
    q_{k+1} = q_k ∘ exp(w_k · dt/2)       # 四元数增量，右乘（机体系）
    a_k = R(q_mid) · f_k + g              # 世界系线加速度（g 为重力加速度向量）
    v_{k+1} = v_k + a_k · dt
    p_{k+1} = p_k + v_k · dt + 1/2 · a_k · dt^2

明确的范围限制：
- 不估计偏置（``gyro_bias`` / ``accel_bias`` 由调用方给定且全程固定）；
- 不做 SLAM、不做回环、不融合其他传感器；
- 假设重力向量在世界系中恒定。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

from .quaternion import quat_multiply, quat_normalize, quat_rotate
from .validation import prepare_samples


@dataclass
class IntegrationResult:
    """逐样本积分轨迹与汇总指标。"""

    times: np.ndarray
    positions: np.ndarray  # N x 3，世界系位置 (m)
    velocities: np.ndarray  # N x 3，世界系速度 (m/s)
    orientations: np.ndarray  # N x 4，单位四元数 [w,x,y,z]
    accel_world: np.ndarray  # N x 3，世界系线加速度（已减重力），首样本为初始瞬时值
    gravity: np.ndarray
    gyro_bias: np.ndarray
    accel_bias: np.ndarray
    intervals: int = 0

    @property
    def duration(self) -> float:
        return float(self.times[-1] - self.times[0]) if len(self.times) >= 2 else 0.0

    @property
    def final_position(self) -> np.ndarray:
        return self.positions[-1]

    @property
    def final_velocity(self) -> np.ndarray:
        return self.velocities[-1]

    @property
    def final_orientation(self) -> np.ndarray:
        return self.orientations[-1]

    @property
    def path_length(self) -> np.ndarray:
        return float(np.sum(np.linalg.norm(np.diff(self.positions, axis=0), axis=1)))

    def to_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的字典（含逐样本轨迹）。"""
        return {
            "summary": {
                "samples": int(len(self.times)),
                "intervals": self.intervals,
                "duration_s": self.duration,
                "final_position_m": self.final_position.tolist(),
                "final_velocity_mps": self.final_velocity.tolist(),
                "final_orientation_xyzw": _xyzw(self.final_orientation),
                "path_length_m": self.path_length,
            },
            "gravity_mps2": self.gravity.tolist(),
            "gyro_bias_radps": self.gyro_bias.tolist(),
            "accel_bias_mps2": self.accel_bias.tolist(),
            "trajectory": [
                {
                    "t": float(self.times[i]),
                    "position_m": self.positions[i].tolist(),
                    "velocity_mps": self.velocities[i].tolist(),
                    "orientation_xyzw": _xyzw(self.orientations[i]),
                    "accel_world_mps2": self.accel_world[i].tolist(),
                }
                for i in range(len(self.times))
            ],
        }


def _xyzw(q: np.ndarray) -> list[float]:
    return [float(q[1]), float(q[2]), float(q[3]), float(q[0])]


def _delta_quaternion(omega: np.ndarray, dt: float) -> np.ndarray:
    """机体系角速率 omega 在 dt 内的增量四元数 exp(omega*dt/2)。"""
    half_angle = 0.5 * float(np.linalg.norm(omega)) * dt
    if half_angle < 1e-12:
        # 小角速率时用一阶展开，避免除零
        v = 0.5 * omega * dt
        return quat_normalize(np.array([1.0, v[0], v[1], v[2]]))
    s = math.sin(half_angle)
    axis = omega / np.linalg.norm(omega)
    return np.array([math.cos(half_angle), axis[0] * s, axis[1] * s, axis[2] * s])


def integrate_imu(
    samples: Sequence[dict[str, Any]] | None = None,
    *,
    gravity: Sequence[float] | np.ndarray = (0.0, 0.0, -9.81),
    gyro_bias: Sequence[float] | np.ndarray = (0.0, 0.0, 0.0),
    accel_bias: Sequence[float] | np.ndarray = (0.0, 0.0, 0.0),
    initial_position: Sequence[float] | np.ndarray = (0.0, 0.0, 0.0),
    initial_velocity: Sequence[float] | np.ndarray = (0.0, 0.0, 0.0),
    initial_orientation: Sequence[float] | np.ndarray = (1.0, 0.0, 0.0, 0.0),
    t: np.ndarray | None = None,
    gyro: np.ndarray | None = None,
    accel: np.ndarray | None = None,
    **prepare_kwargs: Any,
) -> IntegrationResult:
    """对 IMU 样本做离线积分。

    样本可通过 ``samples``（dict 列表，内部经
    :func:`~imu_integrator.validation.prepare_samples` 校验）传入，
    也可直接传入已校验的 ``t`` / ``gyro`` / ``accel`` 数组
    （gyro 必须已是 rad/s）。``prepare_kwargs`` 透传 ``gyro_unit``、
    ``max_dt``、``sort``、``drop_duplicates``、``gyro_limit_rad_s``。
    """
    if samples is not None:
        t_arr, gyro_arr, accel_arr = prepare_samples(samples, **prepare_kwargs)
    else:
        if t is None or gyro is None or accel is None:
            raise ValueError("必须提供 samples，或同时提供 t/gyro/accel 数组")
        t_arr = np.asarray(t, dtype=float)
        gyro_arr = np.asarray(gyro, dtype=float)
        accel_arr = np.asarray(accel, dtype=float)
        if not (t_arr.ndim == 1 and gyro_arr.shape == (len(t_arr), 3)
                and accel_arr.shape == (len(t_arr), 3)):
            raise ValueError("t 必须为一维长度 N，gyro/accel 必须为 N x 3")
        if len(t_arr) == 0:
            raise ValueError("样本数不能为 0")

    g = np.asarray(gravity, dtype=float)
    bg = np.asarray(gyro_bias, dtype=float)
    ba = np.asarray(accel_bias, dtype=float)
    p = np.asarray(initial_position, dtype=float)
    v = np.asarray(initial_velocity, dtype=float)
    q = quat_normalize(np.asarray(initial_orientation, dtype=float))
    if g.shape != (3,) or bg.shape != (3,) or ba.shape != (3,):
        raise ValueError("gravity/gyro_bias/accel_bias 必须是长度 3 的向量")
    if p.shape != (3,) or v.shape != (3,):
        raise ValueError("initial_position/initial_velocity 必须是长度 3 的向量")

    n = len(t_arr)
    positions = np.empty((n, 3))
    velocities = np.empty((n, 3))
    orientations = np.empty((n, 4))
    accel_world = np.empty((n, 3))

    positions[0] = p
    velocities[0] = v
    orientations[0] = q
    accel_world[0] = quat_rotate(q, accel_arr[0] - ba) + g

    intervals = 0
    for k in range(1, n):
        dt = float(t_arr[k] - t_arr[k - 1])
        omega = gyro_arr[k - 1] - bg
        dq = _delta_quaternion(omega, dt)
        q_new = quat_multiply(q, dq)
        # 区间中点姿态，用于旋转比力（中点法，一阶积分精度更好）
        q_mid = quat_multiply(q, _delta_quaternion(omega, 0.5 * dt))
        f_body = accel_arr[k - 1] - ba
        a_world = quat_rotate(q_mid, f_body) + g

        p = p + v * dt + 0.5 * a_world * dt * dt
        v = v + a_world * dt
        q = q_new

        positions[k] = p
        velocities[k] = v
        orientations[k] = q
        accel_world[k] = quat_rotate(q, accel_arr[k] - ba) + g
        intervals += 1

    return IntegrationResult(
        times=t_arr,
        positions=positions,
        velocities=velocities,
        orientations=orientations,
        accel_world=accel_world,
        gravity=g,
        gyro_bias=bg,
        accel_bias=ba,
        intervals=intervals,
    )
