"""位姿积分器：曲线（等曲率圆弧）运动模型。

每个采样间隔内假设机器人做等曲率运动（圆弧），
对直线（d_theta ≈ 0）退化为直线积分。相比一阶欧拉
（先平移后旋转）在中等转速下精度显著更高，且对
原地旋转、圆弧轨迹是精确的。
"""

from __future__ import annotations

import math

# 直线/圆弧切换阈值：转角小于该值时按直线处理，避免 0/0。
_STRAIGHT_EPSILON_RAD = 1e-12


def wrap_angle(theta: float) -> float:
    """将角度归一化到 (-pi, pi]。"""
    wrapped = (theta + math.pi) % (2.0 * math.pi) - math.pi
    # 边界：-pi 归为 +pi，保持 (-pi, pi] 约定
    return math.pi if wrapped == -math.pi else wrapped


def arc_step(
    x: float,
    y: float,
    theta: float,
    d_left_m: float,
    d_right_m: float,
    track_width_m: float,
) -> tuple[float, float, float]:
    """单步等曲率圆弧积分。

    Args:
        x, y, theta: 起始位姿（米，米，弧度）。
        d_left_m: 左轮本轮线位移（米，可负表示倒车）。
        d_right_m: 右轮本轮线位移（米）。
        track_width_m: 轴距（米）。

    Returns:
        (x_new, y_new, theta_new)，theta_new 已归一化到 (-pi, pi]。
    """
    d_theta = (d_right_m - d_left_m) / track_width_m
    d_s = (d_right_m + d_left_m) / 2.0

    if abs(d_theta) < _STRAIGHT_EPSILON_RAD:
        x_new = x + d_s * math.cos(theta)
        y_new = y + d_s * math.sin(theta)
    else:
        radius = d_s / d_theta
        x_new = x + radius * (math.sin(theta + d_theta) - math.sin(theta))
        y_new = y - radius * (math.cos(theta + d_theta) - math.cos(theta))

    return x_new, y_new, wrap_angle(theta + d_theta)
