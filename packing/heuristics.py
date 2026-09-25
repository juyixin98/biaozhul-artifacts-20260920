"""条带装箱启发式（**非最优**，严禁声明为全局最优）。

实现两种经典启发式并取其优：

1. :func:`bottom_left` —— Bottom-Left（BL）：矩形依次放到"尽可能低、
   再尽可能靠左"的合法位置。位置候选只取已放矩形的右边 x 坐标（含 0），
   在候选 x 处把 y 抬到所有水平相交矩形之上。对 5 种排序各跑一次取最优。
2. :func:`ffdh` —— First-Fit Decreasing Height：按高降序，放入第一个放得下
   的水平货架，否则开新货架。

复杂度：BL 每个矩形 O(m^2) 扫描候选位置，整体 O(n^3)；FFDH 为 O(n^2)。
在 :data:`~packing.bounds.MAX_RECTANGLES` 规模内足够快。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .bounds import Instance
from .geometry import EPS, intervals_overlap


@dataclass
class HeuristicResult:
    """启发式布局结果。"""

    x: np.ndarray            # 各矩形（原下标顺序）左下角 x
    y: np.ndarray            # 各矩形（原下标顺序）左下角 y
    height: float            # 布局占用高度 = max(y_i + h_i)
    method: str              # 取得该结果所用启发式描述
    guarantee: str = "heuristic (not proven optimal)"


def _dedup_candidates(values: list[float]) -> list[float]:
    """对候选坐标排序并按 EPS 去重（不同边产生的重合坐标只留一个）。"""
    out: list[float] = []
    for v in sorted(values):
        if not out or abs(v - out[-1]) > EPS:
            out.append(v)
    return out


def bottom_left(inst: Instance, order: np.ndarray, label: str) -> HeuristicResult:
    """按给定矩形顺序 ``order`` 执行 Bottom-Left 放置。"""
    W = inst.strip_width
    w = inst.widths
    h = inst.heights

    # 已放置矩形（按放置顺序）的原下标与坐标
    px: list[float] = []
    py: list[float] = []
    pid: list[int] = []

    x_out = np.zeros(inst.n, dtype=np.float64)
    y_out = np.zeros(inst.n, dtype=np.float64)

    for idx in order.tolist():
        wi, hi = float(w[idx]), float(h[idx])
        # 候选 x：0 与每个已放矩形的右边；去掉超出条带的
        cands = [0.0]
        for k in range(len(px)):
            xr = px[k] + float(w[pid[k]])
            if xr + wi <= W + EPS:
                cands.append(max(0.0, xr))
        best_x = None
        best_y = None
        for xc in _dedup_candidates(cands):
            # 在 xc 处，y 必须高于所有与之水平正长度重叠的已放矩形
            yc = 0.0
            for k in range(len(px)):
                j = pid[k]
                if intervals_overlap(xc, xc + wi, px[k], px[k] + float(w[j])):
                    top = py[k] + float(h[j])
                    if top > yc:
                        yc = top
            # Bottom-Left：先最小化 y，再最小化 x
            if best_y is None or yc < best_y - EPS or (
                abs(yc - best_y) <= EPS and xc < best_x
            ):
                best_y, best_x = yc, xc
        assert best_x is not None  # x=0 必为合法候选（宽度已在校验时保证）
        x_out[idx] = best_x
        y_out[idx] = best_y
        px.append(best_x)
        py.append(best_y)
        pid.append(idx)

    height = float(np.max(y_out + h))
    return HeuristicResult(x=x_out, y=y_out, height=height, method="bottom-left/" + label)


def ffdh(inst: Instance) -> HeuristicResult:
    """First-Fit Decreasing Height 货架启发式。"""
    W = inst.strip_width
    w = inst.widths
    h = inst.heights
    order = np.argsort(-h, kind="stable")  # 高度降序

    x_out = np.zeros(inst.n, dtype=np.float64)
    y_out = np.zeros(inst.n, dtype=np.float64)

    # 每个货架：[起始 y, 货架高度(=首件高度), 已用宽度]
    shelf_y: list[float] = []
    shelf_top: list[float] = []
    shelf_used: list[float] = []

    for idx in order.tolist():
        wi, hi = float(w[idx]), float(h[idx])
        placed = False
        for s in range(len(shelf_y)):
            if shelf_used[s] + wi <= W + EPS and hi <= shelf_top[s] - shelf_y[s] + EPS:
                x_out[idx] = shelf_used[s]
                y_out[idx] = shelf_y[s]
                shelf_used[s] += wi
                placed = True
                break
        if not placed:
            y0 = shelf_top[-1] if shelf_top else 0.0
            shelf_y.append(y0)
            shelf_top.append(y0 + hi)
            shelf_used.append(wi)
            x_out[idx] = 0.0
            y_out[idx] = y0

    height = shelf_top[-1] if shelf_top else 0.0
    return HeuristicResult(x=x_out, y=y_out, height=float(height), method="ffd-height")


def best_heuristic(inst: Instance) -> HeuristicResult:
    """运行多种排序的 BL 与 FFDH，返回占用高度最小的布局。

    注意：即使在这些候选中最优，结果仍是 **启发式**，不保证全局最优；
    它只会 >= 真实最优高度（也一定 >= 有效下界）。
    """
    n = inst.n
    w, h = inst.widths, inst.heights

    orders: list[tuple[np.ndarray, str]] = [
        (np.argsort(-h, kind="stable"), "height-desc"),
        (np.argsort(-w, kind="stable"), "width-desc"),
        (np.argsort(-(w * h), kind="stable"), "area-desc"),
        (np.argsort(-(w + h), kind="stable"), "max-side-desc"),
        (np.arange(n), "input-order"),
    ]
    results = [bottom_left(inst, order, label) for order, label in orders]
    results.append(ffdh(inst))

    best = min(results, key=lambda r: r.height)
    return best
