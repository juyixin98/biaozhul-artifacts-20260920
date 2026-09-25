"""有向图数据结构。

内部用整数下标 0..n-1 表示节点，边存为 NumPy 数组（head/time/cost），
邻接表 ``adj[u]`` 保存从 u 出发的边在下标数组中的位置。
原始节点 id（字符串或整数）保存在 ``raw_ids`` 中，输出时原样返回。

允许：
* 重边（同一对节点之间多条边，可有不同权重）；
* 自环；
* 零权边与零权环（由求解算法保证终止）。

不允许：负权、NaN/Inf 权重。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .errors import RequestError


@dataclass(frozen=True)
class Edge:
    """一条有向边（供库调用方直接构造图时使用）。"""

    u: int
    v: int
    time: float
    cost: float
    id: str | None = None


class Graph:
    def __init__(
        self,
        n: int,
        edge_uv: np.ndarray,
        edge_time: np.ndarray,
        edge_cost: np.ndarray,
        edge_ids: list[str | None],
        raw_ids: list,
    ):
        self.n = int(n)
        # edge_*: 长度 m 的数组
        self.edge_u = np.asarray(edge_uv[:, 0], dtype=np.int64)
        self.edge_v = np.asarray(edge_uv[:, 1], dtype=np.int64)
        self.time = np.asarray(edge_time, dtype=np.float64)
        self.cost = np.asarray(edge_cost, dtype=np.float64)
        self.m = len(self.edge_u)
        self.edge_ids: list[str | None] = list(edge_ids)
        self.raw_ids = list(raw_ids)

        # 邻接表：边下标
        self.adj: list[np.ndarray] = [
            np.nonzero(self.edge_u == u)[0].astype(np.int64)
            for u in range(self.n)
        ]
        # 反向邻接表（下界 Dijkstra 用）
        rev: list[list[int]] = [[] for _ in range(self.n)]
        for e in range(self.m):
            rev[int(self.edge_v[e])].append(e)
        self.radj: list[np.ndarray] = [
            np.asarray(lst, dtype=np.int64) for lst in rev
        ]

    # ---- 构造辅助 --------------------------------------------------------
    @staticmethod
    def build(nodes: list, edges: list[dict]) -> "Graph":
        """从已校验的 JSON 风格结构构造图。"""
        id_to_idx = {}
        raw_ids = []
        for i, node in enumerate(nodes):
            id_to_idx[node] = i
            raw_ids.append(node)

        uv = np.empty((len(edges), 2), dtype=np.int64)
        times = np.empty(len(edges), dtype=np.float64)
        costs = np.empty(len(edges), dtype=np.float64)
        edge_ids: list[str | None] = []
        seen_edge_ids: set[str] = set()

        for k, e in enumerate(edges):
            u = id_to_idx[e["source"]]
            v = id_to_idx[e["target"]]
            uv[k, 0] = u
            uv[k, 1] = v
            times[k] = float(e["time"])
            costs[k] = float(e["cost"])
            eid = e.get("id")
            if eid is not None:
                if eid in seen_edge_ids:
                    raise RequestError(
                        "duplicate_edge_id",
                        f"边 id {eid!r} 重复，边 id 必须唯一",
                        f"graph.edges[{k}].id",
                    )
                seen_edge_ids.add(eid)
            edge_ids.append(eid)

        return Graph(len(nodes), uv, times, costs, edge_ids, raw_ids)

    def raw_node(self, idx: int):
        return self.raw_ids[int(idx)]

    def edge_ref(self, edge_index: int):
        """边在响应中的表示：提供了 id 就用 id，否则用 u->v[k]。"""
        eid = self.edge_ids[int(edge_index)]
        if eid is not None:
            return eid
        u = self.raw_ids[int(self.edge_u[edge_index])]
        v = self.raw_ids[int(self.edge_v[edge_index])]
        # 重边时用序号消歧：u->v 的第几条
        siblings = np.nonzero(
            (self.edge_u == self.edge_u[edge_index])
            & (self.edge_v == self.edge_v[edge_index])
        )[0]
        ordinal = int(np.searchsorted(siblings, edge_index))
        return {"source": u, "target": v, "index": ordinal}
