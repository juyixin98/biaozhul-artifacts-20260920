"""布局合法性校验：出界检测 + 两两碰撞检测（NumPy 向量化）。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .bounds import Instance
from .geometry import EPS, placements_collide


@dataclass
class LayoutReport:
    ok: bool
    height: float
    collisions: list[tuple[int, int]]
    out_of_bounds: list[int]
    negative_coordinates: list[int]

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "height": self.height,
            "collisions": [list(p) for p in self.collisions],
            "out_of_bounds": self.out_of_bounds,
            "negative_coordinates": self.negative_coordinates,
        }


def verify_layout(inst: Instance, x: np.ndarray, y: np.ndarray) -> LayoutReport:
    """检查布局是否：不出条带左右边界、坐标非负、两两不重叠。

    布局高度单独按 ``max(y_i + h_i)`` 报告；本函数不要求布局高度等于
    某个给定值（调用方自行比较）。共边（边重合）按允许处理。
    """
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    w, h, W = inst.widths, inst.heights, inst.strip_width

    out_of_bounds = [int(i) for i in np.nonzero(x + w > W + EPS)[0]]
    neg = [int(i) for i in np.nonzero((x < -EPS) | (y < -EPS))[0]]
    collisions = placements_collide(x, y, w, h)
    height = float(np.max(y + h))

    return LayoutReport(
        ok=not out_of_bounds and not neg and not collisions,
        height=height,
        collisions=collisions,
        out_of_bounds=out_of_bounds,
        negative_coordinates=neg,
    )
