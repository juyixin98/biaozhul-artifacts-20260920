"""简单路径的暴力枚举（仅用于测试与小图校验）。

用带 visited 集合的深度优先搜索枚举 source -> target 的全部简单路径，
再按容差做 Pareto 过滤。上限：24 节点 / 200 边 / 200000 条路径
（见 errors.py），超限抛出 ``EnumerationLimit``。

零权环不影响枚举：visited 集合天然禁止重复节点。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .errors import ENUM_MAX_EDGES, ENUM_MAX_NODES, ENUM_MAX_PATHS, MosppError
from .graph import Graph
from .tolerance import pareto_mask


class EnumerationLimit(MosppError):
    """枚举路径数超过上限。"""


@dataclass
class EnumerationResult:
    points: np.ndarray  # (K,2) Pareto 目标向量
    paths: list[list[int]]  # 与 points 对齐的节点序列
    edges_used: list[list[int]]
    total_simple_paths: int  # Pareto 过滤前的简单路径总数


def enumerate_simple_paths(
    graph: Graph,
    source: int,
    target: int,
    *,
    atol: float = 1e-9,
    rtol: float = 1e-9,
    time_budget: float | None = None,
    cost_budget: float | None = None,
    max_paths: int = ENUM_MAX_PATHS,
) -> EnumerationResult:
    if graph.n > ENUM_MAX_NODES or graph.m > ENUM_MAX_EDGES:
        raise EnumerationLimit(
            f"暴力枚举仅支持 <= {ENUM_MAX_NODES} 节点 / "
            f"<= {ENUM_MAX_EDGES} 边的图"
        )

    all_points: list[tuple[float, float]] = []
    all_paths: list[list[int]] = []
    all_edges: list[list[int]] = []

    visited = np.zeros(graph.n, dtype=bool)
    path_nodes: list[int] = [source]
    path_edges: list[int] = []
    visited[source] = True
    totals = [0]

    def tol_for(b: float) -> float:
        return atol + rtol * abs(b)

    def dfs(u: int, t: float, c: float) -> None:
        if u == target:
            if totals[0] >= max_paths:
                raise EnumerationLimit(
                    f"枚举到 {max_paths} 条简单路径仍未结束"
                )
            all_points.append((t, c))
            all_paths.append(list(path_nodes))
            all_edges.append(list(path_edges))
            totals[0] += 1
            return
        for e in graph.adj[u]:
            v = int(graph.edge_v[e])
            if visited[v]:
                continue
            nt = t + float(graph.time[e])
            nc = c + float(graph.cost[e])
            # 预算剪枝（简单路径上权重非负，前缀已超预算则无需继续）
            if time_budget is not None and nt > time_budget + tol_for(time_budget):
                continue
            if cost_budget is not None and nc > cost_budget + tol_for(cost_budget):
                continue
            visited[v] = True
            path_nodes.append(v)
            path_edges.append(int(e))
            dfs(v, nt, nc)
            path_edges.pop()
            path_nodes.pop()
            visited[v] = False

    dfs(source, 0.0, 0.0)

    if not all_points:
        return EnumerationResult(
            points=np.empty((0, 2)),
            paths=[],
            edges_used=[],
            total_simple_paths=0,
        )

    pts = np.array(all_points, dtype=np.float64)
    mask = pareto_mask(pts, atol, rtol)
    idxs = np.nonzero(mask)[0]

    # 按 (time, cost) 排序，方便与标签算法对照
    order = idxs[np.lexsort((pts[idxs, 1], pts[idxs, 0]))]
    return EnumerationResult(
        points=pts[order],
        paths=[all_paths[i] for i in order],
        edges_used=[all_edges[i] for i in order],
        total_simple_paths=totals[0],
    )
