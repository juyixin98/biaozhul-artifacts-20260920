"""穷举求解器：在联合状态空间上做一致代价搜索，用于核对 CBS 的最优性。

仅适用于小规模实例（2~3 个机器人、小地图）。语义与 CBS 完全一致：
到达目标的机器人永久占格（冻结），代价为各机器人到达时刻之和。
"""

from __future__ import annotations

import heapq
from collections import deque
from itertools import product

import numpy as np

from .grid import MOVES, Cell, is_free


def _moves_fn(grid: np.ndarray):
    cache: dict[Cell, list[Cell]] = {}

    def moves(pos: Cell, arrived: bool) -> list[Cell]:
        if arrived:
            return [pos]
        if pos not in cache:
            opts = [pos]
            for dr, dc in MOVES:
                nxt = (pos[0] + dr, pos[1] + dc)
                if is_free(grid, nxt):
                    opts.append(nxt)
            cache[pos] = opts
        return cache[pos]

    return moves


def _joint_ok(prev: tuple[Cell, ...], nxt: tuple[Cell, ...], n: int) -> bool:
    # 顶点冲突（含撞上已冻结在目标的机器人）
    if len(set(nxt)) != n:
        return False
    # 对向换边
    for i in range(n):
        for j in range(i + 1, n):
            if nxt[i] == prev[j] and nxt[j] == prev[i] and prev[i] != prev[j]:
                return False
    return True


def joint_feasible(
    grid: np.ndarray,
    starts: list[Cell],
    goals: list[Cell],
    max_states: int = 500000,
) -> bool | None:
    """联合状态空间 BFS 可行性判定：True 可解，False 无解，None 超出状态上限。"""
    n = len(starts)
    starts_t, goals_t = tuple(starts), tuple(goals)
    moves = _moves_fn(grid)
    seen = {starts_t}
    queue = deque([starts_t])
    while queue:
        state = queue.popleft()
        if state == goals_t:
            return True
        if len(seen) > max_states:
            return None
        per_agent = [moves(state[i], state[i] == goals_t[i]) for i in range(n)]
        for combo in product(*per_agent):
            nxt = tuple(combo)
            if nxt not in seen and _joint_ok(state, nxt, n):
                seen.add(nxt)
                queue.append(nxt)
    return False


def brute_force_solve(
    grid: np.ndarray,
    starts: list[Cell],
    goals: list[Cell],
    max_states: int = 500000,
) -> tuple[int, list[list[Cell]]] | None:
    """返回 (最小总代价, 各机器人路径)；无解或超限返回 None。"""
    n = len(starts)
    starts_t = tuple(starts)
    goals_t = tuple(goals)
    moves = _moves_fn(grid)

    # 一致代价搜索：每推进一步，代价增加“尚未到达目标的机器人数”
    open_heap: list[tuple[int, int, tuple[Cell, ...]]] = [(0, 0, starts_t)]
    best: dict[tuple[Cell, ...], int] = {starts_t: 0}
    parent: dict[tuple[Cell, ...], tuple[Cell, ...]] = {}
    counter = 0
    states_expanded = 0

    while open_heap:
        cost, _, state = heapq.heappop(open_heap)
        if best.get(state) != cost:
            continue
        if state == goals_t:
            # 重建各机器人路径
            joint = [state]
            while joint[-1] in parent:
                joint.append(parent[joint[-1]])
            joint.reverse()
            paths = [[step[i] for step in joint] for i in range(n)]
            return cost, paths
        states_expanded += 1
        if states_expanded > max_states:
            return None
        step_cost = sum(1 for i in range(n) if state[i] != goals_t[i])
        per_agent = [moves(state[i], state[i] == goals_t[i]) for i in range(n)]
        for combo in product(*per_agent):
            nxt = tuple(combo)
            if not _joint_ok(state, nxt, n):
                continue
            new_cost = cost + step_cost
            if new_cost < best.get(nxt, 1 << 60):
                best[nxt] = new_cost
                parent[nxt] = state
                counter += 1
                heapq.heappush(open_heap, (new_cost, counter, nxt))
    return None
