"""二维栅格精确欧氏距离变换（EDT）。

算法：Felzenszwalb & Huttenlocher 的抛物线包络两遍法，
先行后列各做一次一维精确平方距离变换，总复杂度 O(rows * cols)，
结果为精确欧氏距离（非曼哈顿/切角近似），并同时追踪最近障碍来源。

支持：
- 非方形栅格尺寸（行、列方向分辨率可不同）；
- 全空地图（无障碍：距离为 inf，来源为 -1）；
- 全障碍地图（距离全为 0，来源为自身）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

INF = math.inf


def _dt1d(f: np.ndarray, scale: float) -> tuple[np.ndarray, np.ndarray]:
    """一维精确平方距离变换（Felzenszwalb-Huttenlocher）。

    d[p] = min_q( scale * (p - q)^2 + f[q] )，arg[p] 为取到最小值的 q。

    参数:
        f: 一维浮点数组，可含 inf（表示该位置不产生抛物线）。
        scale: 平方距离的权重（即该方向栅格尺寸的平方）。
    返回:
        (d, arg)：平方距离数组与来源下标数组；f 全为 inf 时
        d 全为 inf、arg 全为 -1。
    """
    n = f.shape[0]
    d = np.full(n, INF, dtype=np.float64)
    arg = np.full(n, -1, dtype=np.int64)
    v = np.empty(n, dtype=np.int64)   # 包络中抛物线的位置
    z = np.empty(n + 1, dtype=np.float64)  # 相邻抛物线的分界点
    k = -1  # 包络中最后一条抛物线的下标，-1 表示包络为空

    for q in range(n):
        fq = float(f[q])
        if math.isinf(fq):
            continue
        if k == -1:
            k = 0
            v[0] = q
            z[0] = -INF
            z[1] = INF
            continue
        while True:
            p = int(v[k])
            s = ((fq + scale * q * q) - (float(f[p]) + scale * p * p)) / (
                2.0 * scale * (q - p)
            )
            if s <= z[k]:
                k -= 1
                if k == -1:
                    # 弹空了：q 的抛物线成为唯一一条
                    k = 0
                    v[0] = q
                    z[0] = -INF
                    z[1] = INF
                    break
            else:
                k += 1
                v[k] = q
                z[k] = s
                z[k + 1] = INF
                break

    if k == -1:
        return d, arg

    j = 0
    for p in range(n):
        while z[j + 1] < p:
            j += 1
        q = int(v[j])
        d[p] = scale * (p - q) ** 2 + float(f[q])
        arg[p] = q
    return d, arg


@dataclass(frozen=True)
class DistanceField:
    """距离场结果。

    属性:
        distances: (rows, cols) 浮点数组，每格到最近障碍的欧氏距离；
            地图全空时为 inf。
        source_rows / source_cols: (rows, cols) 整型数组，最近障碍栅格的
            行/列下标；地图全空时为 -1。障碍格的来源是其自身。
        cell_size: (dy, dx)，行方向与列方向的栅格尺寸（米）。
    """

    distances: np.ndarray
    source_rows: np.ndarray
    source_cols: np.ndarray
    cell_size: tuple[float, float]

    @property
    def shape(self) -> tuple[int, int]:
        return self.distances.shape  # type: ignore[return-value]


def euclidean_distance_transform(
    occupancy: np.ndarray, cell_size: tuple[float, float] = (1.0, 1.0)
) -> DistanceField:
    """计算占据栅格的精确欧氏距离变换。

    参数:
        occupancy: 二维数组，非零/True 表示障碍，0/False 表示自由。
        cell_size: (dy, dx)，行、列方向的栅格尺寸，必须为正数。
    返回:
        DistanceField。
    """
    grid = np.asarray(occupancy)
    if grid.ndim != 2:
        raise ValueError(f"occupancy 必须是二维数组，实际维度为 {grid.ndim}")
    dy, dx = float(cell_size[0]), float(cell_size[1])
    if not (math.isfinite(dy) and dy > 0) or not (math.isfinite(dx) and dx > 0):
        raise ValueError(f"cell_size 必须为正有限数，实际为 {cell_size}")

    rows, cols = grid.shape
    obstacle = grid.astype(bool)

    # 第一遍：沿行方向（列内），尺度 dy^2
    col_sq = np.full((rows, cols), INF, dtype=np.float64)
    col_src = np.full((rows, cols), -1, dtype=np.int64)
    seeds = np.where(obstacle, 0.0, INF)
    for c in range(cols):
        d, a = _dt1d(seeds[:, c], dy * dy)
        col_sq[:, c] = d
        col_src[:, c] = a

    # 第二遍：沿列方向（行内），尺度 dx^2，并回溯来源坐标
    sq = np.full((rows, cols), INF, dtype=np.float64)
    source_rows = np.full((rows, cols), -1, dtype=np.int64)
    source_cols = np.full((rows, cols), -1, dtype=np.int64)
    for r in range(rows):
        d, a = _dt1d(col_sq[r, :], dx * dx)
        sq[r, :] = d
        for c in range(cols):
            q = int(a[c])
            if q >= 0:
                source_rows[r, c] = col_src[r, q]
                source_cols[r, c] = q

    return DistanceField(
        distances=np.sqrt(sq),
        source_rows=source_rows,
        source_cols=source_cols,
        cell_size=(dy, dx),
    )
