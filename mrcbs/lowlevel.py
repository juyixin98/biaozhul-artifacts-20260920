"""CBS 低层：带时空约束的单机器人 A*。

约束形式：
- 顶点约束 (cell, t)：t 时刻不得位于 cell；
- 边约束 (frm, to, t)：不得在 [t, t+1] 区间内从 frm 移动到 to。

目标到达语义：机器人到达目标后永久占格，因此到达时刻 t 必须满足
目标格在任意 t' >= t 都没有顶点约束。
"""

from __future__ import annotations

import heapq
import math

import numpy as np

from .grid import MOVES, Cell, is_free

VertexConstraint = tuple[Cell, int]
EdgeConstraint = tuple[Cell, Cell, int]


class LowLevelFailure(Exception):
    """给定约束下找不到可行路径（或超出时间上限）。"""


def _time_horizon(grid: np.ndarray, dist: np.ndarray, start: Cell,
                  vertex_constraints: set[VertexConstraint],
                  edge_constraints: set[EdgeConstraint]) -> int | None:
    """时间上限：自由格数 + 最大约束时刻 + 2。

    时间扩展图中任何不重复经过同一 (cell, t) 的最短路都不会超过该上界；
    起点不可达目标时返回 None。
    """
    if math.isinf(dist[start]):
        return None
    max_ct = 0
    for _, t in vertex_constraints:
        max_ct = max(max_ct, t)
    for _, _, t in edge_constraints:
        max_ct = max(max_ct, t)
    n_free = int((grid == 0).sum())
    return n_free + max_ct + 2


def constrained_astar(
    grid: np.ndarray,
    dist: np.ndarray,
    start: Cell,
    goal: Cell,
    vertex_constraints: set[VertexConstraint] | None = None,
    edge_constraints: set[EdgeConstraint] | None = None,
    max_time: int | None = None,
) -> list[Cell]:
    """返回从 start 到 goal 的逐时刻格子序列（含起终点），失败抛 LowLevelFailure。

    max_time 为允许的最大到达时刻；缺省时取“自由格数 + 最大约束时刻 + 2”，
    时间扩展图中的最短路不会超过该上界。
    """
    vertex_constraints = vertex_constraints or set()
    edge_constraints = edge_constraints or set()

    horizon = max_time
    if horizon is None:
        horizon = _time_horizon(grid, dist, start, vertex_constraints, edge_constraints)
    if horizon is None:
        raise LowLevelFailure("起点无法到达目标")
    if math.isinf(dist[start]):
        raise LowLevelFailure("起点无法到达目标")

    # 目标格被顶点约束覆盖的最大时刻；到达时刻必须大于它（到达后永久占格）
    goal_blocked_until = -1
    for cell, t in vertex_constraints:
        if cell == goal:
            goal_blocked_until = max(goal_blocked_until, t)

    if (start, 0) in vertex_constraints:
        raise LowLevelFailure("起点在 t=0 被约束")

    # 堆元素: (f, g, 计数器, cell)
    counter = 0
    open_heap: list[tuple[float, int, int, Cell]] = []
    heapq.heappush(open_heap, (dist[start], 0, counter, start))
    best_g: dict[tuple[Cell, int], int] = {(start, 0): 0}
    parent: dict[tuple[Cell, int], tuple[Cell, int]] = {}

    while open_heap:
        _, g, _, cell = heapq.heappop(open_heap)
        state = (cell, g)
        if best_g.get(state) != g:
            continue  # 过期堆元素
        if cell == goal and g > goal_blocked_until:
            # 重建路径
            path = [cell]
            while state in parent:
                state = parent[state]
                path.append(state[0])
            path.reverse()
            return path
        if g >= horizon:
            continue
        t_next = g + 1
        # 到达目标后永久占格：位于目标格时只允许等待（不得离开再返回）
        if cell == goal:
            candidates = [cell]
        else:
            candidates = [cell] + [(cell[0] + dr, cell[1] + dc) for dr, dc in MOVES]
        for nxt in candidates:
            if not is_free(grid, nxt):
                continue
            if (nxt, t_next) in vertex_constraints:
                continue
            if (cell, nxt, g) in edge_constraints:
                continue
            nstate = (nxt, t_next)
            if t_next < best_g.get(nstate, math.inf):
                best_g[nstate] = t_next
                parent[nstate] = state
                counter += 1
                heapq.heappush(open_heap, (t_next + dist[nxt], t_next, counter, nxt))

    raise LowLevelFailure("约束下无可行路径")
