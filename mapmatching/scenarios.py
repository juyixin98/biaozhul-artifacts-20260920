"""合成测试场景与离线轨迹生成（无真实硬件、无外部数据）。"""

from __future__ import annotations

import numpy as np

from .graph import Edge, RoadGraph
from .matcher import Observation


def build_parallel_roads() -> RoadGraph:
    """两条平行道路（间距 60 米），端点处有连接，双向通行。"""
    nodes = {0: (0.0, 0.0), 1: (1000.0, 0.0), 2: (0.0, 60.0), 3: (1000.0, 60.0)}
    bottom = np.array([[0.0, 0.0], [1000.0, 0.0]])
    top = np.array([[0.0, 60.0], [1000.0, 60.0]])
    left_conn = np.array([[0.0, 0.0], [0.0, 60.0]])
    right_conn = np.array([[1000.0, 0.0], [1000.0, 60.0]])
    edges = [
        Edge("bottom_E", 0, 1, bottom),
        Edge("bottom_W", 1, 0, bottom[::-1]),
        Edge("top_E", 2, 3, top),
        Edge("top_W", 3, 2, top[::-1]),
        Edge("left_N", 0, 2, left_conn),
        Edge("left_S", 2, 0, left_conn[::-1]),
        Edge("right_N", 1, 3, right_conn),
        Edge("right_S", 3, 1, right_conn[::-1]),
    ]
    return RoadGraph(nodes, edges)


def build_overpass() -> RoadGraph:
    """立交无连接场景：水平路与竖直路几何相交但无共享节点（不同标高）。

    水平边 h_E/h_W 连接节点 0↔1（y=0），竖直边 v_N/v_S 连接节点 2↔3（x=0）。
    两族边在 (0,0) 处几何交叉但拓扑上完全不连通。
    """
    nodes = {
        0: (-500.0, 0.0),
        1: (500.0, 0.0),
        2: (0.0, -500.0),
        3: (0.0, 500.0),
    }
    horiz = np.array([[-500.0, 0.0], [500.0, 0.0]])
    vert = np.array([[0.0, -500.0], [0.0, 500.0]])
    edges = [
        Edge("h_E", 0, 1, horiz),
        Edge("h_W", 1, 0, horiz[::-1]),
        Edge("v_N", 2, 3, vert),
        Edge("v_S", 3, 2, vert[::-1]),
    ]
    return RoadGraph(nodes, edges)


def noisy_along_line(
    start: tuple[float, float],
    end: tuple[float, float],
    n: int,
    dt: float,
    sigma: float,
    rng: np.random.Generator,
    noise_axes: tuple[bool, bool] = (True, True),
    t0: float = 0.0,
    gap_after: int | None = None,
    gap_seconds: float = 0.0,
) -> list[Observation]:
    """沿直线等间隔生成带高斯噪声的观测；可在指定点后插入时间断档。"""
    start = np.asarray(start, dtype=float)
    end = np.asarray(end, dtype=float)
    fracs = np.linspace(0.0, 1.0, n)
    obs = []
    for k, f in enumerate(fracs):
        p = start + f * (end - start)
        if noise_axes[0]:
            p = p + np.array([0.0, rng.normal(0, sigma)])
        if noise_axes[1]:
            p = p + np.array([rng.normal(0, sigma), 0.0])
        t = t0 + k * dt + (gap_seconds if gap_after is not None and k > gap_after else 0.0)
        obs.append(Observation(t=float(t), x=float(p[0]), y=float(p[1])))
    return obs
