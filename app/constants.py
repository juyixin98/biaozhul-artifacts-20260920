"""默认配置与常量。

单位约定（全部显式可配置）：
- 输入加速度单位默认 ``ms2``（m/s²）；内部统一转换为 m/s²。
- 输入角速度单位默认 ``rads``（rad/s）；内部统一转换为 rad/s。
- 输入时间单位默认 ``s``；内部统一转换为秒。

坐标轴：内部统一使用右手坐标系 ``X-Y-Z``（即输入顺序即 XYZ）。
若原始数据坐标轴顺序/方向不同，可通过 ``axes`` 配置重映射
（见 :func:`imu_bias.config.ConfigModel.to_runtime_config`）。
"""

from __future__ import annotations

import numpy as np

STANDARD_GRAVITY = 9.80665  # m/s^2

# 支持的单位名 -> 换算到 SI 的系数（乘法）
ACCEL_UNIT_TO_MS2: dict[str, float] = {
    "ms2": 1.0,
    "g": STANDARD_GRAVITY,
}
GYRO_UNIT_TO_RADS: dict[str, float] = {
    "rads": 1.0,
    "degs": float(np.pi / 180.0),
    "rad_h": 1.0 / 3600.0,
    "deg_h": float(np.pi / 180.0) / 3600.0,
    "rpm": float(2.0 * np.pi / 60.0),
}
TIME_UNIT_TO_S: dict[str, float] = {
    "s": 1.0,
    "ms": 1e-3,
    "us": 1e-6,
    "ns": 1e-9,
}

# 合法轴映射：内部 X/Y/Z <- 输入列（带符号）
CANONICAL_AXES = ("+x", "-x", "+y", "-y", "+z", "-z")
