"""合成传感器数据处理：对带噪声的二维位置采样做线性轨迹最小二乘拟合。

传感器数据为合成生成（见 examples），不来自真实硬件。将
points(t) = center + velocity * t 拟合为 CircleBody 所需的
初始圆心与常速度，随后交给 CCD 核心做解析碰撞分析——拟合只是
参数估计，碰撞判定本身仍完全基于解析求根。
"""

from __future__ import annotations

import numpy as np

from .models import CircleBody


def fit_linear_trajectory(
    samples: np.ndarray,
    radius: float,
) -> CircleBody:
    """由 (t, x, y) 采样最小二乘拟合匀速直线运动的圆盘。

    参数
    ----
    samples : shape=(N, 3)，每行 [t, x, y]，至少 2 个采样点
    radius  : 圆盘半径

    返回
    ----
    CircleBody，center 为 t=0 拟合位置，velocity 为拟合速度。
    """
    arr = np.asarray(samples, dtype=float)
    if arr.ndim != 2 or arr.shape[1] != 3:
        raise ValueError("采样数据形状必须为 (N, 3)：每行 [t, x, y]")
    if arr.shape[0] < 2:
        raise ValueError("线性轨迹拟合至少需要 2 个采样点")

    t = arr[:, 0]
    xy = arr[:, 1:]
    # 设计矩阵 [1, t]；正规方程等价 np.linalg.lstsq
    design = np.column_stack([np.ones_like(t), t])
    coeff, *_ = np.linalg.lstsq(design, xy, rcond=None)
    center = coeff[0]
    velocity = coeff[1]
    return CircleBody(center=center, velocity=velocity, radius=float(radius))


def fit_from_samples(
    robot_samples: np.ndarray,
    robot_radius: float,
    obstacle_samples: np.ndarray,
    obstacle_radius: float,
) -> tuple:
    """同时拟合机器人与单个障碍，返回 (robot, obstacle)。"""
    robot = fit_linear_trajectory(robot_samples, robot_radius)
    obstacle = fit_linear_trajectory(obstacle_samples, obstacle_radius)
    return robot, obstacle
