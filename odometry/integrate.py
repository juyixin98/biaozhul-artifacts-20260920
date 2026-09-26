"""差速里程计位姿积分（曲线/圆弧模型）。

运动模型：每个采样间隔内假设机器人做等曲率运动（圆弧），
直行是曲率为零的退化情形。相比一阶欧拉（先平移后旋转），
圆弧积分在恒定轮速下对任意步长都是精确的。

记单步左、右轮位移 ds_l, ds_r（由编码器增量换算），则：

    ds     = (ds_r + ds_l) / 2        机器人中心弧长
    dtheta = (ds_r - ds_l) / L        航向变化，L 为轴距

位姿更新（世界系，theta 为积分前航向）：

    dtheta != 0:  R = ds / dtheta
        x += R * (sin(theta + dtheta) - sin(theta))
        y -= R * (cos(theta + dtheta) - cos(theta))
    dtheta == 0:
        x += ds * cos(theta)
        y += ds * sin(theta)
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# |dtheta| 小于该值时按直行退化处理，避免 R = ds/dtheta 数值不稳定
_STRAIGHT_EPS = 1e-12


@dataclass(frozen=True)
class Pose2D:
    """二维位姿（世界系）。"""

    x: float
    y: float
    theta: float

    def as_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "theta": self.theta}


def wheel_distances(
    tick_deltas_left: np.ndarray,
    tick_deltas_right: np.ndarray,
    wheel_diameter_left: float,
    wheel_diameter_right: float,
    ticks_per_revolution: float,
) -> tuple:
    """把编码器步增量换算为左右轮滚过距离 (m)。"""
    meters_per_tick_left = np.pi * wheel_diameter_left / ticks_per_revolution
    meters_per_tick_right = np.pi * wheel_diameter_right / ticks_per_revolution
    ds_left = np.asarray(tick_deltas_left, dtype=np.float64) * meters_per_tick_left
    ds_right = np.asarray(tick_deltas_right, dtype=np.float64) * meters_per_tick_right
    return ds_left, ds_right


def integrate_differential(
    ds_left: np.ndarray,
    ds_right: np.ndarray,
    track_width: float,
    initial_pose: Pose2D = Pose2D(0.0, 0.0, 0.0),
) -> np.ndarray:
    """按圆弧模型积分左右轮位移序列，返回逐步位姿。

    Args:
        ds_left: 形状 (N,) 的左轮每步位移 (m)，ds_left[0] 通常为 0。
        ds_right: 形状 (N,) 的右轮每步位移 (m)。
        track_width: 轴距 L (m)。
        initial_pose: 初始位姿。

    Returns:
        形状 (N, 3) 的数组，每行为 [x, y, theta]，第 0 行为初始位姿。
        theta 归一化到 (-pi, pi]。
    """
    ds_left = np.asarray(ds_left, dtype=np.float64)
    ds_right = np.asarray(ds_right, dtype=np.float64)
    if ds_left.shape != ds_right.shape:
        raise ValueError("左右轮位移序列形状必须一致")
    n = ds_left.shape[0]

    poses = np.zeros((n, 3), dtype=np.float64)
    if n == 0:
        return poses
    poses[0] = [initial_pose.x, initial_pose.y, initial_pose.theta]

    ds_center = 0.5 * (ds_right + ds_left)
    dtheta = (ds_right - ds_left) / track_width

    for i in range(1, n):
        theta_prev = poses[i - 1, 2]
        ds = ds_center[i]
        dth = dtheta[i]
        if abs(dth) < _STRAIGHT_EPS:
            dx = ds * np.cos(theta_prev)
            dy = ds * np.sin(theta_prev)
        else:
            radius = ds / dth
            dx = radius * (np.sin(theta_prev + dth) - np.sin(theta_prev))
            dy = -radius * (np.cos(theta_prev + dth) - np.cos(theta_prev))
        x = poses[i - 1, 0] + dx
        y = poses[i - 1, 1] + dy
        theta = _normalize_angle(theta_prev + dth)
        poses[i] = [x, y, theta]
    return poses


def _normalize_angle(angle: float) -> float:
    """归一化到 (-pi, pi]。"""
    return float((angle + np.pi) % (2.0 * np.pi) - np.pi)
