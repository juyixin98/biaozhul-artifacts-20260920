"""独立的网格暴力法（仅用于测试中交叉验证精确求解器）。

与 :mod:`packing.exact` 刻意采用 *不同* 的策略，以避免同一处思维错误在
"对照"中被复制：

* 位置候选是整数网格上的 **每一个** 单元 ``(x, y)``，``0 <= x <= W-w``、
  ``0 <= y <= H-h``，而不是已放矩形的边；
* **不做** 稳定性剪枝、不做面积剪枝之外的任何推导；
* 碰撞判断直接逐对调用几何原语。

因此它更慢、上限更小（n <= 6 且 W、H <= 20），但逻辑直白，适合作为
穷举真相（ground truth）核对候选位置 DFS 的结果。
"""

from __future__ import annotations

import numpy as np

from .bounds import Instance
from .geometry import EPS, rects_overlap

GRID_MAX_RECTS = 6
GRID_MAX_COORD = 20


def grid_feasible(inst: Instance, H: float) -> bool:
    """暴力判断整数尺寸矩形能否以整数坐标放进 W × H（不旋转）。"""
    W = inst.strip_width
    w = np.rint(inst.widths).astype(int)
    h = np.rint(inst.heights).astype(int)
    Wi, Hi = int(round(W)), int(round(H))
    n = len(w)

    if n > GRID_MAX_RECTS or Wi > GRID_MAX_COORD or Hi > GRID_MAX_COORD:
        raise ValueError("网格暴力法仅支持 n<=%d 且 W,H<=%d"
                         % (GRID_MAX_RECTS, GRID_MAX_COORD))
    if float(np.sum(inst.widths * inst.heights)) > W * H + EPS:
        return False

    # 面积以外的唯一排序优化：大件先放（纯加速，不影响完备性）
    order = list(np.argsort(-inst.heights, kind="stable"))

    placed: list[tuple[int, int, int, int]] = []  # (x, y, w, h)

    def dfs(k: int) -> bool:
        if k == n:
            return True
        idx = order[k]
        wi, hi = int(w[idx]), int(h[idx])
        for xc in range(0, Wi - wi + 1):
            for yc in range(0, Hi - hi + 1):
                if any(rects_overlap(xc, yc, wi, hi, *p) for p in placed):
                    continue
                placed.append((xc, yc, wi, hi))
                if dfs(k + 1):
                    return True
                placed.pop()
        return False

    return dfs(0)


def grid_optimal_height(inst: Instance, h_max: int | None = None) -> int | None:
    """从面积下界（向上取整）起逐高度暴力，返回最小可行整数高度。"""
    W = inst.strip_width
    area_lb = int(np.ceil(float(np.sum(inst.widths * inst.heights)) / W - 1e-9))
    max_h = h_max if h_max is not None else int(round(float(np.sum(inst.heights))))
    for H in range(area_lb, max_h + 1):
        if grid_feasible(inst, float(H)):
            return H
    return None
