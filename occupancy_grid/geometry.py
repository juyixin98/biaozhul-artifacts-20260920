"""几何工具:栅格内射线遍历(Bresenham)与二维位姿变换。"""

from __future__ import annotations

import math


def bresenham_cells(
    row0: int, col0: int, row1: int, col1: int
) -> list[tuple[int, int]]:
    """返回从 (row0, col0) 到 (row1, col1) 的 Bresenham 直线经过的全部
    单元(含起点与终点),按从起点到终点的顺序排列。"""
    cells: list[tuple[int, int]] = []
    d_row = abs(row1 - row0)
    d_col = abs(col1 - col0)
    row, col = row0, col0
    step_row = 1 if row1 > row0 else -1
    step_col = 1 if col1 > col0 else -1

    if d_col >= d_row:
        err = d_col // 2
        for _ in range(d_col + 1):
            cells.append((row, col))
            col += step_col
            err -= d_row
            if err < 0:
                row += step_row
                err += d_col
    else:
        err = d_row // 2
        for _ in range(d_row + 1):
            cells.append((row, col))
            row += step_row
            err -= d_col
            if err < 0:
                col += step_col
                err += d_row
    return cells


def transform_point(
    px: float, py: float, pose_x: float, pose_y: float, pose_theta: float
) -> tuple[float, float]:
    """把传感器系下的点 (px, py) 经位姿 (x, y, theta) 变换到世界系。"""
    cos_t = math.cos(pose_theta)
    sin_t = math.sin(pose_theta)
    wx = pose_x + cos_t * px - sin_t * py
    wy = pose_y + sin_t * px + cos_t * py
    return wx, wy
