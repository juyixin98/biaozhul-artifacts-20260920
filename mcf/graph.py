"""有向流网络与残量网络表示。

存储采用经典的“成对弧”邻接表（与 CP 算法书籍中的实现一致）：

- 用户的每条有向边 (u, v, cap, cost) 存为一条前向弧，并自动配一条
  容量为 0、费用为 -cost 的反向弧；
- 平行边天然支持（邻接表中只是多条弧）；
- 自环也允许（自环不影响 s-t 流，且容量 0 的自环不计入负环）。

所有数组使用 NumPy ``int64``（``cap``/`flow` 会在增广中原地更新），
费用累计则在 solver 中用 Python int，避免大总费用溢出。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import limits


@dataclass(frozen=True)
class Edge:
    """用户输入的一条有向边。"""

    index: int   # 在用户边列表中的序号（0 起）
    source: int
    target: int
    capacity: int
    cost: int


class FlowNetwork:
    """容量、费用为整数的有向网络。

    Parameters
    ----------
    n:
        顶点数，顶点编号为 ``0 .. n-1``。
    source, sink:
        源点与汇点。
    edges:
        ``(u, v, capacity, cost)`` 元组的列表。
    """

    def __init__(
        self,
        n: int,
        source: int,
        sink: int,
        edges: list[tuple[int, int, int, int]],
    ) -> None:
        self.n = int(n)
        self.source = int(source)
        self.sink = int(sink)

        self.user_edges: list[Edge] = [
            Edge(i, int(u), int(v), int(cap), int(cost))
            for i, (u, v, cap, cost) in enumerate(edges)
        ]
        m = len(self.user_edges)

        # 前向弧下标 = 2*i，反向弧下标 = 2*i + 1。
        size = 2 * m
        self._from = np.empty(size, dtype=np.int64)
        self._to = np.empty(size, dtype=np.int64)
        self._cap = np.zeros(size, dtype=np.int64)
        self._cost = np.empty(size, dtype=np.int64)
        self._origin = np.empty(size, dtype=np.int64)  # 该弧所属的用户边序号
        self._mate = np.empty(size, dtype=np.int64)    # 成对弧下标
        self._adj: list[list[int]] = [[] for _ in range(self.n)]

        for i, e in enumerate(self.user_edges):
            fwd, rev = 2 * i, 2 * i + 1
            self._from[fwd], self._to[fwd] = e.source, e.target
            self._cap[fwd], self._cost[fwd] = e.capacity, e.cost
            self._origin[fwd], self._mate[fwd] = i, rev

            self._from[rev], self._to[rev] = e.target, e.source
            self._cap[rev], self._cost[rev] = 0, -e.cost
            self._origin[rev], self._mate[rev] = i, fwd

            self._adj[e.source].append(fwd)
            self._adj[e.target].append(rev)

    # ---- 基本只读访问 -------------------------------------------------------
    @property
    def m(self) -> int:
        """用户边（输入弧）条数。"""
        return len(self.user_edges)

    def adj(self, u: int) -> list[int]:
        """顶点 ``u`` 的出弧下标列表（含残量反向弧）。"""
        return self._adj[u]

    def from_node(self, arc: int) -> int:
        return int(self._from[arc])

    def to_node(self, arc: int) -> int:
        return int(self._to[arc])

    def capacity(self, arc: int) -> int:
        return int(self._cap[arc])

    def cost(self, arc: int) -> int:
        return int(self._cost[arc])

    # ---- 残量网络操作 -------------------------------------------------------
    def residual_capacity(self, arc: int) -> int:
        return int(self._cap[arc])

    def push(self, arc: int, amount: int) -> None:
        """沿残量弧 ``arc`` 增广 ``amount`` 单位（更新成对弧容量）。"""
        amount = int(amount)
        self._cap[arc] -= amount
        self._cap[self._mate[arc]] += amount

    # ---- 结果提取 -----------------------------------------------------------
    def edge_flow(self, edge_index: int) -> int:
        """用户边 ``edge_index`` 上的当前流量。

        前向弧的初始容量减去剩余容量即为已发送流量
        （等于其反向弧的残量容量）。
        """
        e = self.user_edges[edge_index]
        return e.capacity - int(self._cap[2 * edge_index])

    def flows(self) -> list[int]:
        """全部用户边上的流量（按输入顺序）。"""
        return [self.edge_flow(i) for i in range(self.m)]

    def total_cost(self) -> int:
        """按 ``sum(flow * cost)`` 直接计算当前总费用（Python 任意精度整数）。"""
        total = 0
        for i, e in enumerate(self.user_edges):
            total += self.edge_flow(i) * e.cost
        return int(total)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"FlowNetwork(n={self.n}, source={self.source}, "
            f"sink={self.sink}, edges={self.m})"
        )

    # ---- 校验（供 API 层调用）----------------------------------------------
    def validate_static(self) -> None:
        """检查顶点编号与数值范围；非法时抛出 :class:`ValueError`。"""
        if not isinstance(self.n, int) or self.n < 2:
            raise ValueError("顶点数 n 必须是 >= 2 的整数")
        if not (0 <= self.source < self.n):
            raise ValueError(f"源点编号越界: source={self.source}, n={self.n}")
        if not (0 <= self.sink < self.n):
            raise ValueError(f"汇点编号越界: sink={self.sink}, n={self.n}")
        if self.source == self.sink:
            raise ValueError("源点与汇点不能相同")
        if self.n > limits.MAX_NODES:
            raise ValueError(f"顶点数超过上限 {limits.MAX_NODES}")
        if self.m > limits.MAX_EDGES:
            raise ValueError(f"边数超过上限 {limits.MAX_EDGES}")

        for e in self.user_edges:
            if not (0 <= e.source < self.n and 0 <= e.target < self.n):
                raise ValueError(f"边 #{e.index} 的顶点编号越界")
            if not (limits.MIN_CAPACITY <= e.capacity <= limits.MAX_CAPACITY):
                raise ValueError(
                    f"边 #{e.index} 容量 {e.capacity} 超出允许范围 "
                    f"[{limits.MIN_CAPACITY}, {limits.MAX_CAPACITY}]"
                )
            if not (limits.MIN_COST <= e.cost <= limits.MAX_COST):
                raise ValueError(
                    f"边 #{e.index} 费用 {e.cost} 超出允许范围 "
                    f"[{limits.MIN_COST}, {limits.MAX_COST}]"
                )
