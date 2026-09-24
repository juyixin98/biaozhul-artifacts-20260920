"""分块（tiling）：把全局点云切成 XY 网格块，保持全局坐标与点 ID。

设计要点：
  * 点坐标始终使用**全局坐标**参与平面拟合，块只是点的子集，不做坐标平移。
    因此任何块的平面方程都直接定义在全局坐标系下。
  * 每个点保留它在输入数组中的**全局下标**（global_id），供重叠块合并
    以及调用方追溯。
  * 网格以点云包围盒左下角为原点；每个点属于一个“核心格”，同时被包含
    在相邻块的重叠范围内。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Tile:
    index: tuple[int, int]
    """网格 (列, 行) 下标。"""

    global_indices: np.ndarray
    """该块包含的全局点下标（已按全局顺序排序）。"""

    core_indices: np.ndarray
    """其中“核心格”内的点下标（global_indices 的子集）——
    用于统计块的归属与规模，重叠带来的邻居点不算本块核心点。"""

    bounds: tuple[float, float, float, float]
    """块实际覆盖的 XY 范围 (xmin, ymin, xmax, ymax)，含重叠。"""


def build_tiles(
    points: np.ndarray,
    *,
    tile_size: float,
    overlap: float,
) -> list[Tile]:
    """按 XY 网格切块。

    tile_size <= 0 时返回单个块（整个点云）。
    """
    n = points.shape[0]
    if tile_size <= 0.0 or n == 0:
        return [
            Tile(
                index=(0, 0),
                global_indices=np.arange(n, dtype=np.int64),
                core_indices=np.arange(n, dtype=np.int64),
                bounds=(
                    float(points[:, 0].min()) if n else 0.0,
                    float(points[:, 1].min()) if n else 0.0,
                    float(points[:, 0].max()) if n else 0.0,
                    float(points[:, 1].max()) if n else 0.0,
                ),
            )
        ]

    xmin, ymin = float(points[:, 0].min()), float(points[:, 1].min())
    xmax, ymax = float(points[:, 0].max()), float(points[:, 1].max())

    nx = max(1, int(np.ceil((xmax - xmin) / tile_size)) + 1)
    ny = max(1, int(np.ceil((ymax - ymin) / tile_size)) + 1)
    # +1 格保证边界点一定有核心格归属（落在格线上的点取下界格）。

    gx = np.clip(np.floor((points[:, 0] - xmin) / tile_size).astype(np.int64), 0, nx - 1)
    gy = np.clip(np.floor((points[:, 1] - ymin) / tile_size).astype(np.int64), 0, ny - 1)

    ext = overlap * tile_size  # 每侧外扩距离

    tiles: list[Tile] = []
    for ix in range(nx):
        for iy in range(ny):
            cx0 = xmin + ix * tile_size
            cy0 = ymin + iy * tile_size
            core = np.where((gx == ix) & (gy == iy))[0]
            if core.size == 0:
                continue
            b0, b1 = cx0 - ext, cx0 + tile_size + ext
            c0, c1 = cy0 - ext, cy0 + tile_size + ext
            member = np.where(
                (points[:, 0] >= b0) & (points[:, 0] <= b1)
                & (points[:, 1] >= c0) & (points[:, 1] <= c1)
            )[0]
            member.sort()
            tiles.append(
                Tile(
                    index=(ix, iy),
                    global_indices=member,
                    core_indices=core,
                    bounds=(float(b0), float(c0), float(b1), float(c1)),
                )
            )
    return tiles
