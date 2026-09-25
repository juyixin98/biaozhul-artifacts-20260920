"""最小费用（最大）流：Successive Shortest Path + 势函数 Dijkstra。

算法
----
1. **初始势函数**：以源点为起点，在初始残量网络（费用可能为负）上跑
   SPFA（队列优化 Bellman–Ford），得到最短路距离作为初始势 ``p``；
   同时检测从源点可达的负费用环——若存在则拒绝（输入前提禁止）。
2. **反复增广**：在残量网络上以 **约化费用** ``c'(u,v)=c(u,v)+p[u]-p[v]``
   跑 Dijkstra（约化费用恒非负），沿最短路按瓶颈容量增广，
   然后 ``p[v] += dist[v]``（仅可达点）更新势函数。
3. 汇点不可达即得到最大流；若给定 ``required_flow`` 且达不到，返回
   ``infeasible``（已增广部分仍是该流量下的最小费用流）。

数值约定
--------
容量、费用、流量均为整数；约化费用、距离、势函数用 ``int64`` 精确运算
（容差 EPS=0，无浮点）。总费用用 Python 任意精度整数累计，避免
``边数 × 容量 × 费用`` 超出 int64。
"""

from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from . import limits
from .graph import FlowNetwork


class NegativeCycleError(ValueError):
    """残量网络中存在从源点可达的负费用环，最小费用流无界。"""


@dataclass
class FlowResult:
    """求解结果。

    Attributes
    ----------
    status:
        ``"optimal"``（达到最大流或恰好达到要求流量）或
        ``"infeasible"``（要求流量超过最大可达流量）。
    flow:
        源点发出（=汇点收到）的总流量。
    cost:
        总费用 ``sum_e flow_e * cost_e``（Python int，任意精度）。
    edge_flows:
        每条用户边上的流量，按输入顺序。
    potentials:
        最终势函数；不可达顶点为 ``None``（JSON 中为 ``null``）。
    iterations:
        增广次数（Dijkstra 调用次数）。
    max_flow_reached:
        是否已增广到最大流（汇点在残量网络中不可达）。
    """

    status: str
    flow: int
    cost: int
    edge_flows: list[int]
    potentials: list[int | None]
    iterations: int
    max_flow_reached: bool
    _network: FlowNetwork = field(repr=False)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "flow": self.flow,
            "cost": self.cost,
            "iterations": self.iterations,
            "max_flow_reached": self.max_flow_reached,
            "potentials": self.potentials,
            "flows": [
                {
                    "edge": i,
                    "from": e.source,
                    "to": e.target,
                    "flow": f,
                    "cost": e.cost,
                }
                for i, (e, f) in enumerate(
                    zip(self._network.user_edges, self.edge_flows)
                )
            ],
        }

    # ---- 结果自检 -----------------------------------------------------------
    def verify(self) -> None:
        """校验容量约束与流守恒；违例时抛出 :class:`AssertionError`。"""
        net = self._network
        balance = [0] * net.n
        for i, e in enumerate(net.user_edges):
            f = self.edge_flows[i]
            assert 0 <= f <= e.capacity, (
                f"边 #{i} 流量 {f} 超出容量 [0, {e.capacity}]"
            )
            balance[e.source] -= f
            balance[e.target] += f
        for v in range(net.n):
            if v == net.source:
                assert balance[v] == -self.flow, (
                    f"源点 {v} 净流出 { -balance[v] } != 总流量 {self.flow}"
                )
            elif v == net.sink:
                assert balance[v] == self.flow, (
                    f"汇点 {v} 净流入 {balance[v]} != 总流量 {self.flow}"
                )
            else:
                assert balance[v] == 0, (
                    f"中间点 {v} 不满足流守恒（净值 {balance[v]}）"
                )


def _initial_potentials(net: FlowNetwork) -> np.ndarray:
    """SPFA 求源点最短路作为初始势函数，并检测可达负环。"""
    n = net.n
    inf = limits.DIST_INF
    dist = np.full(n, inf, dtype=np.int64)
    depth = np.zeros(n, dtype=np.int64)  # 当前最短路的弧数，用于负环判定
    in_queue = np.zeros(n, dtype=bool)

    dist[net.source] = 0
    queue = deque([net.source])
    in_queue[net.source] = True

    while queue:
        u = queue.popleft()
        in_queue[u] = False
        du = int(dist[u])
        for arc in net.adj(u):
            if net.residual_capacity(arc) <= 0:
                continue
            v = net.to_node(arc)
            nd = du + net.cost(arc)
            if nd < int(dist[v]):
                dist[v] = nd
                depth[v] = depth[u] + 1
                if depth[v] >= n:
                    # 最短路含 n 条弧 ⇒ 存在从源点可达的负环
                    raise NegativeCycleError(
                        "检测到从源点可达的负费用环，最小费用流无界"
                    )
                if not in_queue[v]:
                    queue.append(v)
                    in_queue[v] = True
    return dist


def min_cost_max_flow(
    net: FlowNetwork,
    required_flow: int | None = None,
) -> FlowResult:
    """在 ``net`` 上原地计算最小费用（最大）流。

    Parameters
    ----------
    net:
        已通过 :meth:`FlowNetwork.validate_static` 校验的网络。
    required_flow:
        可选的目标流量。``None`` 表示求最大流；否则增广到该值即停，
        达不到时结果状态为 ``infeasible``。
    """
    net.validate_static()
    if required_flow is not None:
        required_flow = int(required_flow)
        if required_flow < 0:
            raise ValueError("required_flow 必须是非负整数")
        if required_flow > limits.MAX_REQUIRED_FLOW:
            raise ValueError(
                f"required_flow 超过上限 {limits.MAX_REQUIRED_FLOW}"
            )

    pot = _initial_potentials(net)  # int64[n]，不可达为 DIST_INF
    inf = limits.DIST_INF

    total_flow = 0
    total_cost = 0  # Python 任意精度整数
    iterations = 0

    while required_flow is None or total_flow < required_flow:
        # ---- Dijkstra（约化费用）------------------------------------------
        dist = np.full(net.n, inf, dtype=np.int64)
        prev_arc = np.full(net.n, -1, dtype=np.int64)
        visited = np.zeros(net.n, dtype=bool)
        dist[net.source] = 0
        heap: list[tuple[int, int]] = [(0, net.source)]

        while heap:
            d, u = heapq.heappop(heap)
            if visited[u]:
                continue
            visited[u] = True
            if u == net.sink:
                break
            du = int(dist[u])
            pu = int(pot[u])
            for arc in net.adj(u):
                cap = net.residual_capacity(arc)
                if cap <= 0:
                    continue
                v = net.to_node(arc)
                pv = int(pot[v])
                if pv >= inf // 2:
                    # 初始不可达点此后也不可能变为可达（新增的残量反向弧
                    # 两端都在已可达集合内），故此处不会出现；防御性跳过。
                    continue
                reduced = net.cost(arc) + pu - pv
                nd = du + reduced
                if nd < int(dist[v]):
                    dist[v] = nd
                    prev_arc[v] = arc
                    heapq.heappush(heap, (nd, v))

        iterations += 1

        if int(dist[net.sink]) >= inf // 2:
            # 汇点不可达 ⇒ 已达最大流
            return _build_result(
                net, pot, total_flow, total_cost, iterations,
                max_flow_reached=True,
                status=(
                    "optimal"
                    if required_flow is None or total_flow == required_flow
                    else "infeasible"
                ),
            )

        # ---- 更新势函数 ----------------------------------------------------
        reached = dist < inf // 2
        pot[reached] += dist[reached]

        # ---- 沿最短路找瓶颈并增广 ------------------------------------------
        target = (
            required_flow - total_flow
            if required_flow is not None
            else None
        )
        bottleneck = target if target is not None else limits.MAX_CAPACITY + 1
        path_cost = 0
        v = net.sink
        while v != net.source:
            arc = int(prev_arc[v])
            if arc < 0:  # 防御性：理论上不会发生
                raise RuntimeError("增广路径断裂（内部错误）")
            cap = net.residual_capacity(arc)
            if cap < bottleneck:
                bottleneck = cap
            path_cost += net.cost(arc)
            v = net.from_node(arc)

        v = net.sink
        while v != net.source:
            arc = int(prev_arc[v])
            net.push(arc, bottleneck)
            v = net.from_node(arc)

        total_flow += bottleneck
        total_cost += bottleneck * path_cost

    # required_flow 恰好达到（此时不一定是最大流）
    return _build_result(
        net, pot, total_flow, total_cost, iterations,
        max_flow_reached=False, status="optimal",
    )


def _build_result(
    net: FlowNetwork,
    pot: np.ndarray,
    total_flow: int,
    total_cost: int,
    iterations: int,
    max_flow_reached: bool,
    status: str,
) -> FlowResult:
    flows = net.flows()
    direct_cost = net.total_cost()
    assert direct_cost == total_cost, (
        f"费用累计不一致：增量累计 {total_cost} != 直接核算 {direct_cost}"
    )
    potentials: list[int | None] = [
        int(x) if int(x) < limits.DIST_INF // 2 else None
        for x in pot
    ]
    return FlowResult(
        status=status,
        flow=total_flow,
        cost=total_cost,
        edge_flows=flows,
        potentials=potentials,
        iterations=iterations,
        max_flow_reached=max_flow_reached,
        _network=net,
    )
