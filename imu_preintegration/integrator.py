"""IMU 离线积分器：已知重力与固定偏置下的姿态/速度/位置积分。

模型约定：
- 世界系重力向量 g（默认 [0, 0, -9.81]）。
- 加速度计测量比力 f = R^T (a_world - g) + b_a，故
  a_world = R(q) @ (f_meas - b_a) + g。
- 陀螺仪测量 omega_meas = omega_true + b_g，角速度单位 rad/s。
- 姿态用四元数 [w, x, y, z] 表示机体系到世界系的旋转，支持可变时间间隔。

明确不做：完整 SLAM、偏置在线估计（偏置由配置以固定值给出）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import quaternion as quat
from .validation import ValidationReport, validate_imu_stream

DEFAULT_GRAVITY = np.array([0.0, 0.0, -9.81])


@dataclass(frozen=True)
class IntegrationConfig:
    """积分配置。偏置为固定值，不做在线估计。"""

    gravity: np.ndarray = field(default_factory=lambda: DEFAULT_GRAVITY.copy())
    gyro_bias: np.ndarray = field(default_factory=lambda: np.zeros(3))
    accel_bias: np.ndarray = field(default_factory=lambda: np.zeros(3))
    max_dt: float = 0.05
    max_gyro_rad_s: float = 4.0 * np.pi

    def __post_init__(self) -> None:
        for name in ("gravity", "gyro_bias", "accel_bias"):
            arr = np.asarray(getattr(self, name), dtype=float)
            if arr.shape != (3,):
                raise ValueError(f"{name} must have shape (3,), got {arr.shape}")
            object.__setattr__(self, name, arr)


@dataclass
class IntegrationResult:
    """积分结果：每个时间戳对应一组状态。"""

    timestamps: np.ndarray  # (N,)
    orientations: np.ndarray  # (N, 4) 四元数 [w, x, y, z]
    velocities: np.ndarray  # (N, 3)
    positions: np.ndarray  # (N, 3)
    validation: ValidationReport
    skipped_intervals: int  # 因非法 dt（<=0）被跳过的区间数

    def to_dict(self) -> dict:
        return {
            "timestamps": self.timestamps.tolist(),
            "orientations": self.orientations.tolist(),
            "velocities": self.velocities.tolist(),
            "positions": self.positions.tolist(),
            "skipped_intervals": self.skipped_intervals,
            "validation": self.validation.to_dict(),
        }


class ImuIntegrator:
    """离线 IMU 积分器（不可变配置，逐次调用互不影响）。"""

    def __init__(self, config: IntegrationConfig | None = None) -> None:
        self._config = config if config is not None else IntegrationConfig()

    @property
    def config(self) -> IntegrationConfig:
        return self._config

    def integrate(
        self,
        timestamps: np.ndarray,
        gyro: np.ndarray,
        accel: np.ndarray,
        *,
        initial_orientation: np.ndarray | None = None,
        initial_velocity: np.ndarray | None = None,
        initial_position: np.ndarray | None = None,
    ) -> IntegrationResult:
        """对一段 IMU 数据流做离线积分。

        参数：
            timestamps: (N,) 秒，可变间隔。
            gyro: (N, 3) rad/s。
            accel: (N, 3) m/s^2（比力测量）。
            initial_orientation: 初始四元数 [w,x,y,z]，默认单位四元数。
            initial_velocity: 初始速度（世界系），默认零。
            initial_position: 初始位置（世界系），默认零。

        返回：
            IntegrationResult。非法区间（dt <= 0）被跳过并计数；
            校验 error 不阻断积分，但会记录在结果的 validation 中。
        """
        timestamps = np.asarray(timestamps, dtype=float)
        gyro = np.asarray(gyro, dtype=float)
        accel = np.asarray(accel, dtype=float)

        report = validate_imu_stream(
            timestamps,
            gyro,
            accel,
            max_dt=self._config.max_dt,
            max_gyro_rad_s=self._config.max_gyro_rad_s,
        )

        n = timestamps.shape[0]
        q = quat.normalize(
            np.asarray(
                initial_orientation if initial_orientation is not None else [1.0, 0.0, 0.0, 0.0],
                dtype=float,
            )
        )
        v = np.asarray(initial_velocity if initial_velocity is not None else np.zeros(3), dtype=float)
        p = np.asarray(initial_position if initial_position is not None else np.zeros(3), dtype=float)

        orientations = np.empty((n, 4))
        velocities = np.empty((n, 3))
        positions = np.empty((n, 3))
        orientations[0] = q
        velocities[0] = v
        positions[0] = p

        skipped = 0
        for k in range(n - 1):
            dt = timestamps[k + 1] - timestamps[k]
            if dt <= 0.0:
                # 重复或回退的时间戳：状态原样携带，不计数积分
                skipped += 1
                orientations[k + 1] = orientations[k]
                velocities[k + 1] = velocities[k]
                positions[k + 1] = positions[k]
                continue

            # 姿态：区间两端陀螺均值修正偏置后积分（恒定角速度下精确）
            omega = 0.5 * (gyro[k] + gyro[k + 1]) - self._config.gyro_bias
            q = quat.normalize(quat.multiply(q, quat.from_rotvec(omega * dt)))

            # 速度/位置：比力去偏置后旋转到世界系，梯形积分
            f_k = accel[k] - self._config.accel_bias
            f_k1 = accel[k + 1] - self._config.accel_bias
            a_k = quat.rotate(orientations[k], f_k) + self._config.gravity
            a_k1 = quat.rotate(q, f_k1) + self._config.gravity
            a_avg = 0.5 * (a_k + a_k1)

            p = p + v * dt + 0.5 * a_avg * dt * dt
            v = v + a_avg * dt

            orientations[k + 1] = q
            velocities[k + 1] = v
            positions[k + 1] = p

        return IntegrationResult(
            timestamps=timestamps,
            orientations=orientations,
            velocities=velocities,
            positions=positions,
            validation=report,
            skipped_intervals=skipped,
        )
