"""CBS（Conflict-Based Search）高层求解器。

语义约定：
- 离散时刻 t = 0,1,2,...，每时刻可四邻移动或等待；
- 禁止同格同刻（顶点冲突）与对向换边（边冲突）；
- 代价为各机器人到达目标时刻之和（sum of individual costs）；
- 到达目标后永久占格。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field

import numpy as np

from .grid import Cell, distance_map, is_free
from .brute_force import joint_feasible
from .lowlevel import (
    EdgeConstraint,
    LowLevelFailure,
    VertexConstraint,
    constrained_astar,
)


@dataclass(frozen=True)
class VertexConflict:
    agents: tuple[int, int]
    cell: Cell
    time: int


@dataclass(frozen=True)
class EdgeConflict:
    agents: tuple[int, int]
    # 机器人 0: a0 -> a1，机器人 1: a1 -> a0，发生在 [time, time+1]
    a0: Cell
    a1: Cell
    time: int


Conflict = VertexConflict | EdgeConflict


def position_at(path: list[Cell], t: int) -> Cell:
    """t 时刻位置；到达目标后永久占格（路径截断处即目标）。"""
    return path[min(t, len(path) - 1)]


def detect_first_conflict(paths: list[list[Cell]]) -> Conflict | None:
    """按 (时间, 机器人对) 顺序找第一个冲突，保证结果确定性。"""
    n = len(paths)
    horizon = max(len(p) for p in paths)
    for t in range(horizon):
        for i in range(n):
            for j in range(i + 1, n):
                pi_t, pj_t = position_at(paths[i], t), position_at(paths[j], t)
                if pi_t == pj_t:
                    return VertexConflict((i, j), pi_t, t)
                pi_n, pj_n = position_at(paths[i], t + 1), position_at(paths[j], t + 1)
                if pi_t == pj_n and pj_t == pi_n and pi_t != pj_t:
                    return EdgeConflict((i, j), pi_t, pj_t, t)
    return None


@dataclass
class CTNode:
    vertex_constraints: dict[int, set[VertexConstraint]]
    edge_constraints: dict[int, set[EdgeConstraint]]
    paths: list[list[Cell]]
    cost: int
    seq: int = field(default=0)

    def total_constraints(self) -> int:
        return sum(len(s) for s in self.vertex_constraints.values()) + \
            sum(len(s) for s in self.edge_constraints.values())


@dataclass
class CBSResult:
    status: str  # "optimal" | "unsolvable" | "aborted"
    paths: list[list[Cell]] | None
    cost: int | None
    stats: dict


def solve_cbs(
    grid: np.ndarray,
    starts: list[Cell],
    goals: list[Cell],
    max_nodes: int = 10000,
) -> CBSResult:
    """CBS 主循环。返回最优解或状态说明，并附约束树统计。"""
    n = len(starts)
    for s, g in zip(starts, goals):
        if not is_free(grid, s) or not is_free(grid, g):
            return CBSResult("invalid", None, None, {"reason": "起点或终点不可通行"})
    if len(set(starts)) != n or len(set(goals)) != n:
        return CBSResult("invalid", None, None, {"reason": "起点或终点存在重复"})

    # 可行性预检：联合状态空间 BFS（语义与 CBS 完全一致）。不可行则无需
    # 展开约束树——否则无解实例的约束集会不断增长、直到节点上限才中止。
    feasible = joint_feasible(grid, starts, goals)
    if feasible is False:
        return CBSResult("unsolvable", None, None,
                         {"reason": "联合状态空间穷举不可达，实例无解",
                          "feasibility_precheck": True,
                          "nodes_expanded": 0, "nodes_generated": 0})

    dist_maps = [distance_map(grid, g) for g in goals]

    # 完备性时间上限：联合状态数上界为 F^n * 2^n（F 自由格数，n 机器人数，
    # 2^n 为“是否已到达”掩码）。若实例可解，必存在不超过该时刻的解；
    # 固定该上限可保证无解实例的约束树有限、搜索必然终止。
    n_free = int((grid == 0).sum())
    time_bound = (n_free ** n) * (2 ** n)

    def plan(agent: int, vc: set[VertexConstraint], ec: set[EdgeConstraint]) -> list[Cell] | None:
        try:
            return constrained_astar(grid, dist_maps[agent], starts[agent], goals[agent],
                                     vc, ec, max_time=time_bound)
        except LowLevelFailure:
            return None

    root_vc: dict[int, set[VertexConstraint]] = {i: set() for i in range(n)}
    root_ec: dict[int, set[EdgeConstraint]] = {i: set() for i in range(n)}
    root_paths = [plan(i, root_vc[i], root_ec[i]) for i in range(n)]
    if any(p is None for p in root_paths):
        return CBSResult("unsolvable", None, None,
                         {"reason": "存在机器人独立不可达", "nodes_expanded": 0, "nodes_generated": 1})
    root = CTNode(root_vc, root_ec, root_paths, sum(len(p) - 1 for p in root_paths), 0)

    open_heap: list[tuple[int, int, CTNode]] = [(root.cost, 0, root)]
    seq = 1
    nodes_generated = 1
    nodes_expanded = 0
    conflicts_detected = 0
    max_open_size = 1

    while open_heap:
        _, _, node = heapq.heappop(open_heap)
        nodes_expanded += 1
        conflict = detect_first_conflict(node.paths)
        if conflict is None:
            return CBSResult(
                "optimal", node.paths, node.cost,
                {
                    "nodes_expanded": nodes_expanded,
                    "nodes_generated": nodes_generated,
                    "max_open_size": max_open_size,
                    "conflicts_detected": conflicts_detected,
                    "constraints_in_solution": node.total_constraints(),
                    "time_bound": time_bound,
                    "feasibility_precheck": feasible is True,
                },
            )
        conflicts_detected += 1
        if nodes_expanded >= max_nodes:
            return CBSResult(
                "aborted", None, None,
                {
                    "reason": f"超过节点上限 {max_nodes}",
                    "nodes_expanded": nodes_expanded,
                    "nodes_generated": nodes_generated,
                    "conflicts_detected": conflicts_detected,
                },
            )

        # 按冲突类型为双方各生成一个子节点
        branches: list[tuple[int, VertexConstraint | EdgeConstraint]] = []
        if isinstance(conflict, VertexConflict):
            i, j = conflict.agents
            branches = [(i, (conflict.cell, conflict.time)),
                        (j, (conflict.cell, conflict.time))]
        else:
            i, j = conflict.agents
            branches = [(i, (conflict.a0, conflict.a1, conflict.time)),
                        (j, (conflict.a1, conflict.a0, conflict.time))]

        for agent, con in branches:
            vc = {k: set(v) for k, v in node.vertex_constraints.items()}
            ec = {k: set(v) for k, v in node.edge_constraints.items()}
            if isinstance(conflict, VertexConflict):
                vc[agent].add(con)  # type: ignore[arg-type]
            else:
                ec[agent].add(con)  # type: ignore[arg-type]
            new_path = plan(agent, vc[agent], ec[agent])
            if new_path is None:
                continue
            new_paths = list(node.paths)
            new_paths[agent] = new_path
            child = CTNode(vc, ec, new_paths,
                           sum(len(p) - 1 for p in new_paths), seq)
            heapq.heappush(open_heap, (child.cost, seq, child))
            seq += 1
            nodes_generated += 1
        max_open_size = max(max_open_size, len(open_heap))

    return CBSResult(
        "unsolvable", None, None,
        {
            "reason": "约束树搜索耗尽，实例无解",
            "nodes_expanded": nodes_expanded,
            "nodes_generated": nodes_generated,
            "max_open_size": max_open_size,
            "conflicts_detected": conflicts_detected,
        },
    )


def pad_timetable(paths: list[list[Cell]]) -> list[list[Cell]]:
    """把各机器人路径补齐到同一时刻表长度（到达后重复目标格）。"""
    makespan = max(len(p) for p in paths) - 1
    return [p + [p[-1]] * (makespan + 1 - len(p)) for p in paths]
