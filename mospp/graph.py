"""有向图数据结构。

图按邻接表存储；弧数组用 NumPy 保存权重。顶点 ID 可以是 JSON 的字符串或整数，
内部映射为连续下标 0..n-1。

无向图（``directed=false``）在内部展开为两条方向相反、权重相同的有向弧，
不做任何特殊处理。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .errors import MOSPError, status
from .tolerance import finite_nonneg


@dataclass(frozen=True)
class Arc:
    """一条有向弧。

    ``key`` 仅用于在输出中区分平行弧（可为 None）。``idx`` 是该弧在图的
    NumPy 权重数组中的下标，求解器通过它从数组取权重。
    """

    u: int
    v: int
    time: float
    cost: float
    key: object = None
    idx: int = -1


class Graph:
    """双目标有向图。"""

    def __init__(self):
        self.node_ids: list = []
        self.index: dict[object, int] = {}
        self.arcs: list[Arc] = []
        self._adj: list[list[Arc]] = []
        # NumPy 权重存储：freeze() 时把全部弧的两维权重固化为 float64 数组，
        # 求解器按下标 arc.idx 取用。
        self._arc_time: np.ndarray | None = None
        self._arc_cost: np.ndarray | None = None

    # ---- 构建 ----
    def add_node(self, node_id) -> int:
        if node_id in self.index:
            return self.index[node_id]
        idx = len(self.node_ids)
        self.index[node_id] = idx
        self.node_ids.append(node_id)
        self._adj.append([])
        return idx

    def add_arc(self, u_id, v_id, time: float, cost: float, key=None) -> Arc:
        u = self.add_node(u_id)
        v = self.add_node(v_id)
        arc = Arc(
            u=u, v=v, time=float(time), cost=float(cost),
            key=key, idx=len(self.arcs),
        )
        self.arcs.append(arc)
        self._adj[u].append(arc)
        return arc

    @property
    def n(self) -> int:
        return len(self.node_ids)

    @property
    def m(self) -> int:
        return len(self.arcs)

    def outgoing(self, u: int):
        return self._adj[u]

    def freeze(self) -> None:
        """把弧权重固化为 NumPy float64 数组（求解前调用）。"""
        m = len(self.arcs)
        if m:
            self._arc_time = np.fromiter(
                (a.time for a in self.arcs), dtype=np.float64, count=m
            )
            self._arc_cost = np.fromiter(
                (a.cost for a in self.arcs), dtype=np.float64, count=m
            )
        else:
            self._arc_time = np.empty(0, dtype=np.float64)
            self._arc_cost = np.empty(0, dtype=np.float64)

    # ---- 从 JSON 请求构建 ----
    @classmethod
    def from_request(cls, req: dict, limits: "SolveLimits") -> "Graph":
        """从请求字典构建图，并在此处完成 **全部结构与范围校验**。"""
        if not isinstance(req, dict):
            raise MOSPError("请求体必须是 JSON 对象", status.INVALID_REQUEST)

        nodes = req.get("nodes")
        edges = req.get("edges")
        if edges is None:
            raise MOSPError("缺少必填字段 edges", status.INVALID_REQUEST)
        if not isinstance(edges, list):
            raise MOSPError("edges 必须是数组", status.INVALID_REQUEST)

        g = cls()

        if nodes is not None:
            if not isinstance(nodes, list) or not nodes:
                raise MOSPError("nodes 若非给出必须是非空数组", status.INVALID_REQUEST)
            if len(nodes) != len(set(_hashable(n) for n in nodes)):
                raise MOSPError("nodes 中存在重复 ID", status.INVALID_REQUEST)
            for nid in nodes:
                _require_node_id(nid)
                g.add_node(nid)

        directed = req.get("directed", True)
        if not isinstance(directed, bool):
            raise MOSPError("directed 必须是布尔值", status.INVALID_REQUEST)

        if len(edges) > limits.max_edges:
            raise MOSPError(
                f"边数 {len(edges)} 超过规模上限 max_edges={limits.max_edges}",
                status.LIMIT_EXCEEDED,
            )

        for i, e in enumerate(edges):
            if not isinstance(e, dict):
                raise MOSPError(f"edges[{i}] 必须是对象", status.INVALID_REQUEST)
            if "from" not in e or "to" not in e:
                raise MOSPError(
                    f"edges[{i}] 缺少 from/to 字段", status.INVALID_REQUEST
                )
            u_id, v_id = e["from"], e["to"]
            _require_node_id(u_id)
            _require_node_id(v_id)
            t = _require_weight(e, "time", i)
            c = _require_weight(e, "cost", i)
            if not (finite_nonneg(t) and finite_nonneg(c)):
                raise MOSPError(
                    f"edges[{i}] 的 time/cost 必须是有限非负数，"
                    f"得到 time={t!r}, cost={c!r}",
                    status.INVALID_REQUEST,
                )
            if nodes is not None and (u_id not in g.index or v_id not in g.index):
                raise MOSPError(
                    f"edges[{i}] 引用了 nodes 中不存在的顶点 {u_id!r}->{v_id!r}",
                    status.INVALID_REQUEST,
                )
            key = e.get("key", i)
            # 无向图展开为两条方向相反的有向弧；平行弧允许，由调用方用 key 区分。
            g.add_arc(u_id, v_id, t, c, key=key)
            if not directed:
                g.add_arc(v_id, u_id, t, c, key=key)

        if g.n > limits.max_nodes:
            raise MOSPError(
                f"顶点数 {g.n} 超过规模上限 max_nodes={limits.max_nodes}",
                status.LIMIT_EXCEEDED,
            )
        if g.n < 1:
            raise MOSPError("图必须至少包含 1 个顶点", status.INVALID_REQUEST)

        source, target = req.get("source"), req.get("target")
        if source is None or target is None:
            raise MOSPError("缺少必填字段 source/target", status.INVALID_REQUEST)
        _require_node_id(source)
        _require_node_id(target)
        if source not in g.index or target not in g.index:
            raise MOSPError(
                f"source/target 必须是图中存在的顶点，得到 {source!r}->{target!r}",
                status.INVALID_REQUEST,
            )

        g.freeze()
        return g


def _hashable(x):
    if isinstance(x, list):
        return ("__list__", tuple(_hashable(i) for i in x))
    return x


def _require_node_id(x) -> None:
    if isinstance(x, bool) or not isinstance(x, (str, int)):
        raise MOSPError(
            f"顶点 ID 必须是字符串或整数（不可为布尔），得到 {x!r}",
            status.INVALID_REQUEST,
        )


def _require_weight(e: dict, name: str, i: int) -> float:
    if name not in e:
        raise MOSPError(
            f"edges[{i}] 缺少 {name} 字段", status.INVALID_REQUEST
        )
    x = e[name]
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise MOSPError(
            f"edges[{i}].{name} 必须是数字，得到 {x!r}",
            status.INVALID_REQUEST,
        )
    return float(x)
