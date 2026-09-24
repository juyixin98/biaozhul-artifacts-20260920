"""道路图：节点、有向边、投影、候选生成与沿路最短路径距离。"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra


@dataclass(frozen=True)
class Node:
    id: str
    x: float
    y: float


@dataclass(frozen=True)
class Edge:
    """有向边。polyline 为形状 (k, 2) 的折线，首末点必须与 u/v 节点坐标一致。"""

    id: str
    u: str  # 起点节点 id
    v: str  # 终点节点 id
    polyline: tuple  # tuple of (x, y)

    @property
    def points(self) -> np.ndarray:
        return np.asarray(self.polyline, dtype=float)


@dataclass
class Candidate:
    """某一观测点在一条边上的投影候选。"""

    edge_id: str
    fraction: float  # 沿边归一化位置 [0, 1]
    distance: float  # 观测点到投影点的垂直距离 (m)
    proj_x: float
    proj_y: float


def _project_point_to_polyline(px: float, py: float, pts: np.ndarray):
    """点投影到折线，返回 (最短距离, fraction, 投影点 x, 投影点 y)。"""
    a = pts[:-1]
    b = pts[1:]
    ab = b - a
    seg_len2 = np.einsum("ij,ij->i", ab, ab)
    seg_len2 = np.where(seg_len2 == 0.0, 1e-12, seg_len2)
    t = ((px - a[:, 0]) * ab[:, 0] + (py - a[:, 1]) * ab[:, 1]) / seg_len2
    t = np.clip(t, 0.0, 1.0)
    proj = a + t[:, None] * ab
    d = np.hypot(proj[:, 0] - px, proj[:, 1] - py)
    i = int(np.argmin(d))
    seg_lens = np.sqrt(np.einsum("ij,ij->i", ab, ab))
    cum = np.concatenate([[0.0], np.cumsum(seg_lens)])
    total = cum[-1]
    if total <= 0.0:
        frac = 0.0
    else:
        frac = float((cum[i] + t[i] * seg_lens[i]) / total)
    return float(d[i]), frac, float(proj[i, 0]), float(proj[i, 1])


class RoadGraph:
    def __init__(self, nodes: list[Node], edges: list[Edge]):
        self.nodes = {n.id: n for n in nodes}
        self.edges = {e.id: e for e in edges}
        self._edge_len: dict[str, float] = {}
        for e in edges:
            pts = e.points
            seg = np.diff(pts, axis=0)
            self._edge_len[e.id] = float(np.sum(np.hypot(seg[:, 0], seg[:, 1])))
        self._build_index()

    def _build_index(self):
        node_ids = sorted(self.nodes)
        self._nidx = {nid: i for i, nid in enumerate(node_ids)}
        n = len(node_ids)
        rows, cols, vals = [], [], []
        # 每个节点的出边，用于后续计算“绕一圈回到本节点”的环路距离
        self._out_edges: dict[str, list[Edge]] = {nid: [] for nid in node_ids}
        for e in self.edges.values():
            i, j = self._nidx[e.u], self._nidx[e.v]
            rows.append(i)
            cols.append(j)
            vals.append(self._edge_len[e.id])
            self._out_edges[e.u].append(e)
        mat = csr_matrix((vals, (rows, cols)), shape=(n, n))
        # 全源最短路（合成场景图很小，直接求全对距离）
        self._dist = dijkstra(mat, directed=True, unweighted=False)
        # loop[i]：从节点出发至少经过一条边再回到本节点的最短距离（dijkstra 对角线为 0，不能直接用）
        self._loop = np.full(n, np.inf)
        for nid, i in self._nidx.items():
            for e in self._out_edges[nid]:
                j = self._nidx[e.v]
                d = self._edge_len[e.id] + self._dist[j, i]
                if d < self._loop[i]:
                    self._loop[i] = d

    def edge_length(self, edge_id: str) -> float:
        return self._edge_len[edge_id]

    def candidates(self, x: float, y: float, radius: float) -> list[Candidate]:
        """按距离生成候选边：投影距离不超过 radius 的边。"""
        out = []
        for e in self.edges.values():
            d, frac, px, py = _project_point_to_polyline(x, y, e.points)
            if d <= radius:
                out.append(Candidate(e.id, frac, d, px, py))
        out.sort(key=lambda c: c.distance)
        return out

    def route_distance(self, a: Candidate, b: Candidate) -> float:
        """从候选 a 的投影点沿图行驶到候选 b 的投影点的最短距离；不可达返回 inf。"""
        ea, eb = self.edges[a.edge_id], self.edges[b.edge_id]
        la, lb = self._edge_len[ea.id], self._edge_len[eb.id]
        if ea.id == eb.id and b.fraction >= a.fraction:
            return (b.fraction - a.fraction) * la
        da = (1.0 - a.fraction) * la  # a 到边 ea 终点
        db = b.fraction * lb  # 边 eb 起点到 b
        i, j = self._nidx[ea.v], self._nidx[eb.u]
        if ea.id == eb.id:
            mid = self._loop[i]  # 同一条边但反向：必须绕环路回来
        else:
            mid = self._dist[i, j]
        return da + mid + db
