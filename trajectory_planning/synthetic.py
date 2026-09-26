"""合成传感器数据：对规划出的轨迹做带噪声的里程计仿真。

数据完全由合成轨迹生成（高斯白噪声），用于离线复核，
不连接任何硬件。
"""

from __future__ import annotations

import numpy as np

from .profile import TrajectoryProfile, sample_grid


def simulate_odometry(
    profile: TrajectoryProfile,
    dt: float = 0.01,
    position_noise_std: float = 0.001,
    speed_noise_std: float = 0.01,
    seed: int = 0,
) -> dict:
    """生成带噪合成里程计读数。

    Args:
        dt: 采样周期（秒）。
        position_noise_std: 位置各分量高斯噪声标准差（米）。
        speed_noise_std: 速度模长噪声标准差（米/秒）。
        seed: 随机种子，保证可复现。

    Returns:
        含 t / position_measured / speed_measured / ground_truth 的 dict。
    """
    if dt <= 0.0:
        raise ValueError("dt 必须为正数")
    rng = np.random.default_rng(seed)
    grid = sample_grid(profile, dt)

    pos_noise = rng.normal(0.0, position_noise_std, size=grid["position"].shape)
    spd_noise = rng.normal(0.0, speed_noise_std, size=grid["speed"].shape)

    return {
        "t": grid["t"],
        "position_ground_truth": grid["position"],
        "position_measured": grid["position"] + pos_noise,
        "speed_ground_truth": grid["speed"],
        "speed_measured": np.clip(grid["speed"] + spd_noise, 0.0, None),
        "noise_std": {
            "position": position_noise_std,
            "speed": speed_noise_std,
        },
        "seed": seed,
    }


def compare_odometry(odo: dict) -> dict:
    """合成读数与真值的误差统计（离线自检用）。"""
    pos_err = np.linalg.norm(
        odo["position_measured"] - odo["position_ground_truth"], axis=1
    )
    spd_err = odo["speed_measured"] - odo["speed_ground_truth"]
    return {
        "position_error_mean": float(pos_err.mean()),
        "position_error_max": float(pos_err.max()),
        "position_error_rmse": float(np.sqrt((pos_err**2).mean())),
        "speed_error_mean": float(spd_err.mean()),
        "speed_error_max": float(np.abs(spd_err).max()),
        "speed_error_rmse": float(np.sqrt((spd_err**2).mean())),
        "n_samples": int(odo["t"].size),
    }
