"""道路图模型：节点、有向边（含折线几何）与最短路（SciPy csgraph）。

坐标使用局部平面坐标（单位：米），合成场景与离线回放均在此坐标系下进行。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra


@dataclass(frozen=True)
class Edge:
    """一条有向道路边。

    id: 边标识
    u, v: 起点节点、终点节点 id
    points: (k, 2) 折线顶点，首点应等于节点 u 坐标，末点等于节点 v 坐标
    """

    id: str
    u: int
    v: int
    points: np.ndarray
    length: float = field(init=False)

    def __post_init__(self) -> None:
        pts = np.asarray(self.points, dtype=float)
        object.__setattr__(self, "points", pts)
        seg = np.diff(pts, axis=0)
        length = float(np.sum(np.hypot(seg[:, 0], seg[:, 1])))
        object.__setattr__(self, "length", length)


class RoadGraph:
    """有向道路图，提供节点最短路距离（沿路网）。"""

    def __init__(self, nodes: dict[int, tuple[float, float]], edges: list[Edge]):
        self.nodes = dict(nodes)
        self.edges = list(edges)
        self.edge_by_id = {e.id: e for e in self.edges}
        node_ids = sorted(self.nodes)
        self._node_index = {nid: i for i, nid in enumerate(node_ids)}
        n = len(node_ids)
        rows, cols, data = [], [], []
        for e in self.edges:
            rows.append(self._node_index[e.u])
            cols.append(self._node_index[e.v])
            data.append(e.length if e.length > 0 else 1e-6)
        self._csr = csr_matrix((data, (rows, cols)), shape=(n, n))
        # 预计算全源最短路（合成图规模小，代价可接受）
        self._dist = dijkstra(self._csr, directed=True, unweighted=False)

    def path_distance(self, u: int, v: int) -> float:
        """节点 u 到节点 v 沿路网的最短距离；不可达返回 np.inf。"""
        return float(self._dist[self._node_index[u], self._node_index[v]])

    def reachable(self, u: int, v: int) -> bool:
        return bool(np.isfinite(self.path_distance(u, v)))
