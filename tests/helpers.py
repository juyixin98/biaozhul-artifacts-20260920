"""测试夹具与随机图生成。"""

from __future__ import annotations

import random

import numpy as np

from mospp.graph import Graph


def make_graph(n: int, edges: list[tuple], nodes=None) -> Graph:
    """用 (u, v, time, cost[, id]) 元组列表构造内部图。"""
    if nodes is None:
        nodes = list(range(n))
    uv = (
        np.array([(e[0], e[1]) for e in edges], dtype=np.int64).reshape(-1, 2)
        if edges
        else np.empty((0, 2), dtype=np.int64)
    )
    edge_ids = [e[4] if len(e) > 4 else None for e in edges]
    return Graph(
        n,
        uv,
        np.array([e[2] for e in edges], dtype=float)
        if edges
        else np.empty(0),
        np.array([e[3] for e in edges], dtype=float)
        if edges
        else np.empty(0),
        edge_ids,
        nodes,
    )


def random_graph(
    rng: random.Random,
    n: int,
    p: float,
    wmax: int = 6,
    zero_cycles: bool = True,
    self_loops: bool = True,
) -> Graph:
    edges = []
    for u in range(n):
        for v in range(n):
            if u != v and rng.random() < p:
                edges.append(
                    (u, v, rng.randint(0, wmax), rng.randint(0, wmax))
                )
    if zero_cycles and n >= 2 and rng.random() < 0.4:
        a, b = rng.sample(range(n), 2)
        edges.append((a, b, 0, 0))
        edges.append((b, a, 0, 0))
    if self_loops and rng.random() < 0.2:
        a = rng.randrange(n)
        edges.append(
            (a, a, rng.randint(0, wmax), rng.randint(0, wmax))
        )
    return make_graph(n, edges)


def front_points(result) -> list[tuple[float, float]]:
    return sorted(
        (round(float(l.time), 9), round(float(l.cost), 9))
        for l in result.labels
    )
