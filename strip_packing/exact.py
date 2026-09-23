"""条带装箱的精确求解：左下角候选点分支定界（仅小规模）。

用途
----
- 为小例提供**精确最优值 H***，用于穷举对照与测试；
- 给出启发式与最优值差距的真实证据。

完备性（为什么枚举有限候选点足以找到最优解）
--------------------------------------------
任取一个最优布局，可将其变换为"左-下稳定"布局：依次把每个矩形
向下、再向左滑动直到触边（总面积不变，不产生重叠，高度不增）。
稳定布局中每个矩形的左下角满足：

    x ∈ {0} ∪ {另一矩形的右边界 x_j + w_j}
    y ∈ {0} ∪ {另一矩形的上边界 y_j + h_j}

"左边贴着谁、下边贴着谁"构成向右、向上的依赖 DAG。搜索每一步
**可以选择任意一个尚未放置的矩形**（而不是固定顺序），因此总能
沿该 DAG 的拓扑序复现稳定最优布局，候选点集合始终包含所需位置。

剪枝
----
- 当前布局最高上沿 ``top`` 不小于已知最好值 -> 剪；
- 全局下界 ``L`` 不小于已知最好值 -> 剪（``L <= H*``）；
- 同尺寸矩形在同一搜索节点只尝试一次（对称性消除）；
- 节点数 / 墙钟时间超限 -> 停止，状态记为 ``limit_reached``，
  此时返回的最好值只是启发式上界，**不声明最优**。

适用规模：自动模式 n <= 6（:data:`strip_packing.solver.EXACT_AUTO_N`）；
显式 ``exact: true`` 可尝试更大实例，但搜索预算耗尽时返回
``status="limit_reached"``，不声明最优。
"""

from __future__ import annotations

import time

import numpy as np

EPS = 1e-9


def exact_packing(rects, strip_width, initial_upper, global_lb=0.0,
                  node_limit: int = 2_000_000,
                  time_limit: float = 10.0):
    """求条带装箱最优高度。

    参数
    ----
    rects:
        ``[(id, w, h), ...]``。
    strip_width:
        条带宽度 W。
    initial_upper:
        已知合法布局高度（来自启发式），作为初始上界。
    global_lb:
        预先计算的全局严格下界。
    node_limit, time_limit:
        搜索预算；超限后 ``status`` 为 ``"limit_reached"``。

    返回
    ----
    dict
        ``{"status", "optimal", "height", "placements", "nodes",
        "elapsed_sec", "node_limit", "time_limit"}``。
        status ∈ ``"empty" | "optimal" | "limit_reached"``。
    """
    W = float(strip_width)
    n = len(rects)
    t0 = time.monotonic()

    if n == 0:
        return {"status": "empty", "optimal": True, "height": 0.0,
                "placements": [], "nodes": 0, "elapsed_sec": 0.0,
                "node_limit": node_limit, "time_limit": time_limit}

    ids = [r[0] for r in rects]
    w = np.array([r[1] for r in rects], dtype=float)
    h = np.array([r[2] for r in rects], dtype=float)

    best_h = float(initial_upper)
    best_at = None
    nodes = 0
    aborted = False

    # 启发式布局已经触达严格下界 => 夹逼成立，无需搜索
    if best_h <= global_lb + EPS:
        return {"status": "optimal", "optimal": True, "height": best_h,
                "placements": None, "nodes": 0,
                "elapsed_sec": time.monotonic() - t0,
                "node_limit": node_limit, "time_limit": time_limit,
                "proved_by": "lower_bound_squeeze"}

    order = sorted(range(n), key=lambda i: (-h[i], -w[i], i))

    # 预算"成对冲突"：wi+wj>W 的矩形对在任何水平线上不能共存。
    conflict = [set() for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if float(w[i]) + float(w[j]) > W + EPS:
                conflict[i].add(j)
                conflict[j].add(i)

    def dfs(remaining_mask, placed, x_edges, y_edges, top, placed_at):
        """placed: [(x,y,right,top), ...]（已放置矩形，纯 Python 列表）"""
        nonlocal best_h, best_at, nodes, aborted
        if aborted:
            return
        nodes += 1
        if nodes > node_limit or ((nodes & 4095) == 0
                                  and time.monotonic() - t0 > time_limit):
            aborted = True
            return
        if remaining_mask == 0:
            if top < best_h - EPS:
                best_h = top
                best_at = list(placed_at)
            return
        if top >= best_h - EPS or global_lb >= best_h - EPS:
            return

        # 局部下界：当前最高上沿 + 剩余矩形之间还必须上下叠放的量。
        # 若剩余中存在一对冲突矩形 (i,j)，它们的垂直区间互不相交，
        # 其中至少一个要落在当前 top 之上 -> 最终高度 >= top + min(hi,hj)。
        local_lb = top
        rem = [i for i in order if remaining_mask & (1 << i)]
        for a in range(len(rem)):
            ia = rem[a]
            for ib in conflict[ia]:
                if remaining_mask & (1 << ib) and ia < ib:
                    cand = top + min(float(h[ia]), float(h[ib]))
                    if cand > local_lb:
                        local_lb = cand
        if local_lb >= best_h - EPS:
            return

        seen_dims = set()
        for idx in order:
            bit = 1 << idx
            if not (remaining_mask & bit):
                continue
            dim = (round(float(w[idx]), 12), round(float(h[idx]), 12))
            if dim in seen_dims:
                continue
            seen_dims.add(dim)
            wi, hi = float(w[idx]), float(h[idx])

            x_list = sorted(x for x in x_edges if x + wi <= W + EPS)
            y_list = sorted(y_edges)

            # 先收集可行位置，按 (y, x) 低优先排序，尽早压低 best_h
            feasible = []
            seen_pos = set()
            for xv in x_list:
                xr = xv + wi
                for yv in y_list:
                    yt = yv + hi
                    if (xv, yv) in seen_pos:
                        continue
                    seen_pos.add((xv, yv))
                    new_top = top if top >= yt else yt
                    if new_top >= best_h - EPS:
                        break  # y 升序，更大的 y 只会更差
                    collide = False
                    for (px, py, pr, pt) in placed:
                        if pr > xv + EPS and px < xr - EPS \
                                and pt > yv + EPS and py < yt - EPS:
                            collide = True
                            break
                    if not collide:
                        feasible.append((yv, xv, yt, xr))
            feasible.sort()

            for yv, xv, yt, xr in feasible:
                new_top = top if top >= yt else yt
                new_edges_x = x_edges | {xr}
                new_edges_y = y_edges | {yt}
                placed.append((xv, yv, xr, yt))
                placed_at.append((idx, xv, yv))
                dfs(remaining_mask ^ bit, placed,
                    new_edges_x, new_edges_y, new_top, placed_at)
                placed.pop()
                placed_at.pop()
                if aborted:
                    return
            if aborted:
                return

    dfs((1 << n) - 1, [], {0.0}, {0.0}, 0.0, [])

    elapsed = time.monotonic() - t0
    if aborted:
        return {"status": "limit_reached", "optimal": False,
                "height": float(best_h), "placements": None,
                "nodes": nodes, "elapsed_sec": elapsed,
                "node_limit": node_limit, "time_limit": time_limit}

    # 搜索正常完成：最优高度即为 best_h。若没有完整布局严格优于启发式
    # 初始上界，说明启发式布局本身已达到最优高度，调用方可继续使用它。
    if best_at is None:
        return {"status": "optimal", "optimal": True,
                "height": float(best_h), "placements": None,
                "nodes": nodes, "elapsed_sec": elapsed,
                "node_limit": node_limit, "time_limit": time_limit,
                "proved_by": "branch_and_bound_value_only"}

    from .geometry import Placement
    placements = [None] * n
    for idx, xv, yv in best_at:
        placements[idx] = Placement(ids[idx], xv, yv,
                                    float(w[idx]), float(h[idx]))
    return {"status": "optimal", "optimal": True, "height": float(best_h),
            "placements": placements, "nodes": nodes,
            "elapsed_sec": elapsed, "node_limit": node_limit,
            "time_limit": time_limit}
