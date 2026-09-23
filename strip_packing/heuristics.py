"""条带装箱启发式（上界）算法。

所有启发式均不旋转矩形，输出的每份布局都经过
:func:`strip_packing.geometry.verify_layout` 独立复核。
启发式给出的高度是最优高度 H* 的 **上界**，本项目任何地方
都不声称启发式结果全局最优。

实现三种经典启发式：

- ``NFDH``  Next Fit Decreasing Height：只在当前层放置，放不下就封层；
- ``FFDH``  First Fit Decreasing Height：放入第一个还能容纳它的层；
- ``BL``    Bottom-Left：在全部已放置矩形的坐标组合点中，选择最靠下、
  再最靠左的可行位置（对有限候选点的标准 bottom-left 实现）。

不同排序（高度降序 / 宽度降序 / 面积降序）分别尝试，取高度最小者。
规模 > ``BL_FULL_MAX_N`` 时只尝试 BL 的高度降序一次，以控制
BL 的 O(n^3) 候选枚举成本。
"""

from __future__ import annotations

import numpy as np

from .geometry import Placement, verify_layout
from .validation import PackingError

BL_FULL_MAX_N = 60


def _snap(v, limit=None, eps=1e-9):
    """把落在边界容差内的坐标吸附到精确边界。"""
    if abs(v) <= eps:
        return 0.0
    if limit is not None and abs(v - limit) <= eps:
        return float(limit)
    return float(v)


def _sorted_rects(rects, key):
    indexed = list(enumerate(rects))
    if key == "height":
        indexed.sort(key=lambda t: (-t[1][2], -t[1][1]))
    elif key == "width":
        indexed.sort(key=lambda t: (-t[1][1], -t[1][2]))
    else:  # area
        indexed.sort(key=lambda t: (-(t[1][1] * t[1][2]), -t[1][2]))
    return indexed


def nfdh(rects, W, key="height"):
    """Next Fit Decreasing Height。返回与输入同序的 Placement 列表。"""
    result = [None] * len(rects)
    shelf_y = 0.0
    shelf_h = 0.0
    x = 0.0
    for orig, (rid, w, h) in _sorted_rects(rects, key):
        if x + w > W and x > 0.0:
            # 当前层放不下（w<=W 已由输入校验保证），封层
            shelf_y += shelf_h
            shelf_h = 0.0
            x = 0.0
        result[orig] = Placement(rid, _snap(x), _snap(shelf_y), w, h)
        x += w
        shelf_h = max(shelf_h, h)
    return result


def ffdh(rects, W, key="height"):
    """First Fit Decreasing Height。返回与输入同序的 Placement 列表。"""
    shelves = []  # [y, used_width, height]
    result = [None] * len(rects)
    for orig, (rid, w, h) in _sorted_rects(rects, key):
        target = None
        for sh in shelves:
            if sh[1] + w <= W + 1e-9:
                target = sh
                break
        if target is None:
            y0 = shelves[-1][0] + shelves[-1][2] if shelves else 0.0
            target = [y0, 0.0, 0.0]
            shelves.append(target)
        y, _, _ = target[0], target[1], target[2]
        result[orig] = Placement(
            rid, _snap(target[1]), _snap(y), w, h)
        target[1] += w
        target[2] = max(target[2], h)
    return result


def bottom_left(rects, W, key="height"):
    """Bottom-Left：在候选 (x,y) 中取最低再最左的可行位置。

    候选 x 取自 {0} ∪ 已放置矩形右边界；对每个候选 x，候选 y
    按升序取自 {0} ∪ 已放置矩形上边界，第一个无碰撞者即为该 x
    下的最低点。取所有候选 x 中 (y, x) 字典序最小的位置。
    """
    result = [None] * len(rects)
    coords = None  # 已放置矩形的 [x, y, right, top] 数组
    for orig, (rid, w, h) in _sorted_rects(rects, key):
        x_cands = [0.0]
        y_cands = [0.0]
        if coords is not None:
            x_cands.extend(coords[:, 2].tolist())
            y_cands.extend(coords[:, 3].tolist())
        x_cands = sorted(set(round(v, 12) for v in x_cands
                             if v <= W - w + 1e-9 or v <= 1e-9))
        y_cands = sorted(set(round(v, 12) for v in y_cands))

        best = None  # (y, x)
        eps = 1e-9
        for xv in x_cands:
            xr = xv + w
            if coords is not None and coords.size:
                # 仅与水平投影区间有重叠的已放置矩形能在垂直方向冲突
                active = (coords[:, 2] > xv + eps) & (coords[:, 0] < xr - eps)
                ac = coords[active]
            else:
                ac = None
            for yv in y_cands:
                yt = yv + h
                if ac is not None and ac.size:
                    clash = ((ac[:, 2] > xv + eps) & (ac[:, 0] < xr - eps)
                             & (ac[:, 3] > yv + eps)
                             & (ac[:, 1] < yt - eps))
                    if np.any(clash):
                        continue
                cand = (yv, xv)
                if best is None or cand < best:
                    best = cand
                break  # y 候选升序，第一个可行即该 x 处最低点

        y, x = best
        p = Placement(rid, _snap(x, None), _snap(y, None), w, h)
        result[orig] = p
        row = np.array([[p.x, p.y, p.right, p.top]])
        coords = row if coords is None else np.vstack([coords, row])
    return result


_HEURISTICS = {
    "NFDH": nfdh,
    "FFDH": ffdh,
    "BL": bottom_left,
}


def run_heuristics(rects, strip_width, tol=None):
    """运行全部启发式变体，返回按高度排序的候选结果列表。

    每个结果为 ``{"name", "height", "placements"}``。每份布局
    均经碰撞/边界复核；复核失败抛 :class:`PackingError`
    （错误码 ``LAYOUT_VERIFICATION_FAILED``，属于内部失败状态）。
    """
    from .geometry import Tolerance
    tol = tol or Tolerance()
    W = float(strip_width)
    n = len(rects)
    keys = ("height", "width", "area") if n <= BL_FULL_MAX_N else ("height",)

    results = []
    # NFDH/FFDH 的标准定义要求矩形按高度非增排序（层高由首个矩形固定）；
    # BL 与排序无关地产生合法布局，因此在 BL 上尝试多种排序。
    plans = [("NFDH", "height"), ("FFDH", "height")]
    plans += [("BL", k) for k in keys]
    for hname, key in plans:
        fn = _HEURISTICS[hname]
        placements = fn(rects, W, key=key)
        report = verify_layout(rects, placements, W, tol)
        if not report["valid"]:
            raise PackingError(
                "LAYOUT_VERIFICATION_FAILED",
                f"启发式 {hname}/{key} 产出非法布局: "
                + "; ".join(report["violations"][:5]))
        results.append({
            "name": f"{hname}-{key}",
            "height": float(report["height"]),
            "placements": placements,
        })
    results.sort(key=lambda r: (r["height"], r["name"]))
    return results
