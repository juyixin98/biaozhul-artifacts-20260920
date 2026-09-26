"""惯性数据预积分子集（IMU offline pre-integration subset）。

纯后端离线计算库：已知重力向量与固定陀螺/加计偏置，对合成 IMU
数据做四元数姿态更新与平移积分。明确不做完整 SLAM，也不在线估计偏置。
"""

from .quaternion import (
    quat_multiply,
    quat_normalize,
    quat_rotate,
    quat_conjugate,
    quat_to_rotation_matrix,
    quat_from_angle_axis,
)
from .integrator import integrate_imu, IntegrationResult
from .validation import (
    validate_samples,
    prepare_samples,
    IMUDataError,
    DEFAULT_GYRO_LIMIT_RAD_S,
    DEFAULT_GYRO_LIMIT_DEG_S,
)
from .synthetic import (
    stationary_samples,
    constant_rotation_samples,
    constant_acceleration_samples,
)

__all__ = [
    "quat_multiply",
    "quat_normalize",
    "quat_rotate",
    "quat_conjugate",
    "quat_to_rotation_matrix",
    "quat_from_angle_axis",
    "integrate_imu",
    "IntegrationResult",
    "validate_samples",
    "prepare_samples",
    "IMUDataError",
    "DEFAULT_GYRO_LIMIT_RAD_S",
    "DEFAULT_GYRO_LIMIT_DEG_S",
    "stationary_samples",
    "constant_rotation_samples",
    "constant_acceleration_samples",
]

__version__ = "0.1.0"
