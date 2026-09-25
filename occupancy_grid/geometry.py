"""几何工具：位姿变换、坐标换算、Bresenham 射线追踪。"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Pose2D:
    """传感器在世界系中的二维位姿。"""

    x: float
    y: float
    theta: float  # 航向角，弧度，逆时针为正


def sensor_to_world(pose: Pose2D, px: float, py: float) -> tuple[float, float]:
    """把传感器系下的点 (px, py) 变换到世界系。"""
    c = math.cos(pose.theta)
    s = math.sin(pose.theta)
    return (pose.x + c * px - s * py, pose.y + s * px + c * py)


def world_to_grid(
    wx: float, wy: float, origin_x: float, origin_y: float, resolution: float
) -> tuple[int, int]:
    """世界坐标 -> 栅格索引 (col, row)。单元 (i, j) 覆盖
    [origin_x + i*res, origin_x + (i+1)*res) x [origin_y + j*res, ...)。

    越界坐标也会返回整数索引，是否有效由调用方按栅格尺寸判断。
    """
    col = math.floor((wx - origin_x) / resolution)
    row = math.floor((wy - origin_y) / resolution)
    return (col, row)


def bresenham(x0: int, y0: int, x1: int, y1: int) -> list[tuple[int, int]]:
    """Bresenham 直线算法，返回从 (x0, y0) 到 (x1, y1)（含两端）的单元序列。"""
    cells: list[tuple[int, int]] = []
    dx = abs(x1 - x0)
    dy = abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx - dy
    x, y = x0, y0
    while True:
        cells.append((x, y))
        if x == x1 and y == y1:
            break
        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            x += sx
        if e2 < dx:
            err += dx
            y += sy
    return cells
