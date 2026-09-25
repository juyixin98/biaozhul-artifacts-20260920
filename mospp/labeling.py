"""双目标（时间、费用）最短路的标签修正算法。

算法概述
========
每条标签记录 (time, cost, node, predecessor_label, edge, visited)。
标签按字典序 (time, cost) 进入最小堆，每次弹出当前时间最小的标签；
时间相同则费用小的先弹出。由于两维权重均非负，被弹出的标签在
*目标向量* 层面不可能被之后弹出的标签支配（后者的时间只会更大），
因此弹出即永久，得到一个标签设定（label-setting）算法。

简单路径（``mode="simple"``，默认）
----------------------------------
每条标签额外保存已访问节点集合 ``visited``。同一节点上两条标签
A、B 的状态支配判定为：

    A 支配 B  <=>  A.obj 弱支配 B.obj 且 A.visited 是 B.visited 的子集

（目标严格相等且 visited 也相等视为重复）。该子集条件保证：被剪枝的
标签能延伸出的简单路径，支配它的标签一定也能延伸（它可选的下一节点
集合是超集），因此剪枝精确，终点处得到精确的简单路径 Pareto 前沿。
visited 集合使状态数呈组合增长，故简单路径模式限定小中规模图
（默认 <= 64 节点 / <= 400 边，见 errors.py）。

允许重复环（``mode="walk"``）
----------------------------
不维护 visited，标签可以含环。由于权重非负，任何含环走法都被去掉环后
的简单走法弱支配，因此其目标向量的 Pareto 前沿与简单路径完全一致；
零权环产生的等权标签作为重复标签被丢弃，算法仍然终止。该模式适合较大图。

标签上限
========
``node_label_cap``：每个节点永久标签数上限；
``total_label_cap``：全局永久标签数上限。
触顶后设置 ``truncated=True`` 并在结果中记录截断位置——此时返回的
Pareto 前沿可能不完整，调用方必须检查该标志。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Optional

import numpy as np

from .graph import Graph
from .tolerance import dominates as obj_dominates
from .tolerance import objectives_equal


@dataclass
class Label:
    __slots__ = ("time", "cost", "node", "pred", "edge", "visited", "lid")
    time: float
    cost: float
    node: int
    pred: int  # 前驱标签在 all_labels 中的下标，-1 表示起点标签
    edge: int  # 到达该标签所用边的下标，-1 表示起点标签
    visited: Optional[frozenset]  # walk 模式为 None
    lid: int


@dataclass
class TruncationRecord:
    node: int
    reason: str
    count: int = 1  # 该原因在此节点上累计丢弃的标签数

    def to_dict(self, graph: Graph) -> dict:
        return {
            "node": graph.raw_node(self.node),
            "reason": self.reason,
            "dropped_labels": self.count,
        }


@dataclass
class ParetoEntry:
    """一个 Pareto 最优点。

    目标向量相同但拓扑不同的路径（仅 simple 模式可能出现多条）
    收集在 ``alternatives`` 中；``label``/``nodes``/``edges`` 是其中第一条。
    """

    time: float
    cost: float
    label: Label
    nodes: list[int]
    edges: list[int]
    alt_nodes: list[list[int]]
    alt_edges: list[list[int]]


@dataclass
class SolveResult:
    status: str  # "ok" | "no_path" | "truncated"
    entries: list[ParetoEntry]  # 去重后的 Pareto 点（按 time 升序）
    truncated: bool
    truncation: list[TruncationRecord]
    stats: dict
    atol: float
    rtol: float
    time_budget: Optional[float]
    cost_budget: Optional[float]

    # 向后兼容的便捷属性
    @property
    def labels(self) -> list[Label]:
        return [e.label for e in self.entries]

    @property
    def paths(self) -> list[list[int]]:
        return [e.nodes for e in self.entries]

    @property
    def edges_used(self) -> list[list[int]]:
        return [e.edges for e in self.entries]


# ---------------------------------------------------------------------------
# 下界：在反向图上分别对时间、费用跑 Dijkstra（非负权重，O((n+m)log n)）
# ---------------------------------------------------------------------------
def _dijkstra_to_target(graph: Graph, weights: np.ndarray, target: int) -> np.ndarray:
    """从每个节点到终点的单目标最短距离数组（不可达为 inf）。

    在 *反向图* 上从 target 出发求得，即 dist[u] = u 到 target 的最短距离。
    零权边完全支持（标准 Dijkstra 即可处理）。
    """
    dist = np.full(graph.n, np.inf, dtype=np.float64)
    dist[target] = 0.0
    heap: list[tuple[float, int]] = [(0.0, target)]
    while heap:
        d, u = heapq.heappop(heap)
        if d != dist[u]:
            continue
        for e in graph.radj[u]:
            p = int(graph.edge_u[e])  # 反向边 p -> u（原图 p->v=u）
            nd = d + float(weights[e])
            if nd < dist[p]:
                dist[p] = nd
                heapq.heappush(heap, (nd, p))
    return dist


# ---------------------------------------------------------------------------
# 同节点状态支配
# ---------------------------------------------------------------------------
def state_dominates(
    a: Label, b: Label, atol: float, rtol: float
) -> bool:
    """永久标签 a 是否支配（或等同重复于）候选/永久标签 b。

    判定规则（A、B 为同节点标签，V 为已访问集合）：

    * **目标严格支配**（两维不劣、一维严格更优）且 ``V_A ⊆ V_B``
      —— B 的任何简单延伸能用的下一节点都在 ``V\\V_B`` 中，
      该集合是 ``V\\V_A`` 的子集，故 B 的每条延伸也是 A 的合法延伸
      且目标严格更优，B 可安全剪枝；
    * **目标在容差内相等**且 ``V_A == V_B`` —— 重复状态，剪枝；
    * 目标相等但 V_A、V_B 不同（含严格子集与不可比两种情形）——
      **不剪枝**：二者可能延伸出不同的等权最优简单路径，
      为保证“枚举对照 / 相等标签覆盖”，这些路径都要保留。

    注意“目标严格支配 + 子集”的剪枝只保证 Pareto *目标点集* 不丢解；
    对目标相等的状态采取最保守的共存策略，使输出能够覆盖枚举得到的
    每一条 Pareto 最优简单路径。
    """
    obj_a = (a.time, a.cost)
    obj_b = (b.time, b.cost)
    strict = obj_dominates(obj_a, obj_b, atol, rtol)
    if strict:
        if a.visited is None:
            return True
        return a.visited.issubset(b.visited)
    if objectives_equal(obj_a, obj_b, atol, rtol):
        if a.visited is None:
            return True  # walk 模式：目标相等即重复走法
        return a.visited == b.visited
    return False


# ---------------------------------------------------------------------------
# 主求解流程
# ---------------------------------------------------------------------------
def solve(
    graph: Graph,
    source: int,
    target: int,
    *,
    mode: str = "simple",
    atol: float = 1e-9,
    rtol: float = 1e-9,
    time_budget: Optional[float] = None,
    cost_budget: Optional[float] = None,
    node_label_cap: int = 10_000,
    total_label_cap: int = 500_000,
) -> SolveResult:
    if mode not in ("simple", "walk"):
        raise ValueError(f"未知模式 {mode!r}，可选 'simple' 或 'walk'")
    use_visited = mode == "simple"

    tol_budget = lambda b: atol + rtol * abs(b) if b is not None else 0.0

    # 下界（仅在对应预算存在时需要，但一起计算代价也很小）
    lb_time = (
        _dijkstra_to_target(graph, graph.time, target)
        if time_budget is not None
        else None
    )
    lb_cost = (
        _dijkstra_to_target(graph, graph.cost, target)
        if cost_budget is not None
        else None
    )

    all_labels: list[Label] = []

    def new_label(
        time: float,
        cost: float,
        node: int,
        pred: int,
        edge: int,
        visited,
    ) -> Label:
        lab = Label(time, cost, node, pred, edge, visited, len(all_labels))
        all_labels.append(lab)
        return lab

    # 起点标签：平凡路径 s->s，权重 (0,0)
    start_vis = frozenset({source}) if use_visited else None
    start = new_label(0.0, 0.0, source, -1, -1, start_vis)

    heap: list[tuple[float, float, int, int]] = []
    # 堆项：(time, cost, 序号, label_id)
    heapq.heappush(heap, (0.0, 0.0, 0, start.lid))
    heap_counter = 1

    permanent: list[list[Label]] = [[] for _ in range(graph.n)]
    permanent_count = 0

    truncation: dict[int, TruncationRecord] = {}

    def record_trunc(node: int, reason: str) -> None:
        rec = truncation.get(node)
        if rec is None:
            truncation[node] = TruncationRecord(node=node, reason=reason)
        else:
            rec.count += 1

    stats = {
        "labels_created": 1,       # 含起点标签、所有候选标签
        "labels_pushed": 1,        # 进入堆的标签
        "labels_popped": 0,        # 从堆弹出的次数
        "labels_rejected": 0,      # 弹出时发现被支配/重复
        "labels_permanent": 0,     # 永久标签数
        "edges_scanned": 0,
        "budget_pruned": 0,
    }
    global_stop = False

    def within_budget(t: float, c: float, node: int) -> bool:
        if time_budget is not None:
            bt = tol_budget(time_budget)
            if t + (lb_time[node] if lb_time is not None else 0.0) > time_budget + bt:
                return False
        if cost_budget is not None:
            bt = tol_budget(cost_budget)
            if c + (lb_cost[node] if lb_cost is not None else 0.0) > cost_budget + bt:
                return False
        return True

    while heap and not global_stop:
        t, c, _, lid = heapq.heappop(heap)
        lab = all_labels[lid]
        stats["labels_popped"] += 1

        # 弹出时再次检查：入堆后可能出现了支配它的永久标签
        dominated_now = False
        for p in permanent[lab.node]:
            if state_dominates(p, lab, atol, rtol):
                dominated_now = True
                break
        if dominated_now:
            stats["labels_rejected"] += 1
            continue

        if len(permanent[lab.node]) >= node_label_cap:
            stats["labels_rejected"] += 1
            record_trunc(lab.node, "node_label_cap")
            continue
        if permanent_count >= total_label_cap:
            record_trunc(lab.node, "total_label_cap")
            global_stop = True
            continue
        permanent[lab.node].append(lab)
        permanent_count += 1
        stats["labels_permanent"] += 1

        # 扩展后继
        for e in graph.adj[lab.node]:
            stats["edges_scanned"] += 1
            w = int(graph.edge_v[e])
            if use_visited and w in lab.visited:
                continue  # 简单路径：禁止成环
            nt = t + float(graph.time[e])
            nc = c + float(graph.cost[e])

            # 预算剪枝（带上界估计，保证不丢解）
            if not within_budget(nt, nc, w):
                stats["budget_pruned"] += 1
                continue

            nvis = (
                frozenset(lab.visited | {w}) if use_visited else None
            )
            cand = new_label(nt, nc, w, lab.lid, int(e), nvis)
            stats["labels_created"] += 1

            # 与该节点已有永久标签比较，能立即判定被支配/重复则不入堆
            pruned = False
            for p in permanent[w]:
                if state_dominates(p, cand, atol, rtol):
                    pruned = True
                    break
            if pruned:
                continue

            heapq.heappush(heap, (nt, nc, heap_counter, cand.lid))
            heap_counter += 1
            stats["labels_pushed"] += 1

    # ---- 终点结果：在永久标签上做目标级 Pareto 过滤 --------------------
    t_labels = permanent[target]
    truncated = global_stop or bool(truncation)

    kept: list[Label] = []
    if t_labels:
        pts = np.array(
            [[l.time, l.cost] for l in t_labels], dtype=np.float64
        )
        from .tolerance import pareto_mask

        mask = pareto_mask(pts, atol, rtol)
        kept = [l for l, keep in zip(t_labels, mask) if keep]
        kept.sort(key=lambda l: (l.time, l.cost))

    # 按容差相等的目标向量分组（simple 模式下可能是拓扑不同的等权路径；
    # walk 模式在状态支配阶段已折叠，通常每组只有一条）。
    entries: list[ParetoEntry] = []
    for lab in kept:
        group = None
        for e in entries:
            if objectives_equal(
                (e.time, e.cost), (lab.time, lab.cost), atol, rtol
            ):
                group = e
                break
        nodes, es = reconstruct(lab, all_labels)
        if group is None:
            entries.append(
                ParetoEntry(
                    time=lab.time,
                    cost=lab.cost,
                    label=lab,
                    nodes=nodes,
                    edges=es,
                    alt_nodes=[],
                    alt_edges=[],
                )
            )
        elif nodes not in [group.nodes, *group.alt_nodes]:
            # 防御性去重：拓扑完全相同的等权路径不重复列出
            group.alt_nodes.append(nodes)
            group.alt_edges.append(es)

    # 全局上限触发时，已返回标签仍有效，但集合可能不完整
    if not entries:
        status = "no_path" if not truncated else "truncated"
    elif truncated:
        status = "truncated"
    else:
        status = "ok"

    stats["nodes_reached"] = sum(1 for lst in permanent if lst)
    stats["permanent_label_total"] = permanent_count

    return SolveResult(
        status=status,
        entries=entries,
        truncated=truncated,
        truncation=sorted(truncation.values(), key=lambda r: r.node),
        stats=stats,
        atol=atol,
        rtol=rtol,
        time_budget=time_budget,
        cost_budget=cost_budget,
    )


def reconstruct(label: Label, all_labels: list[Label]):
    """沿前驱链重建节点序列与边序列（顺序：起点 -> 终点）。"""
    node_rev = [label.node]
    edge_rev: list[int] = []
    cur = label
    while cur.pred != -1:
        edge_rev.append(cur.edge)
        cur = all_labels[cur.pred]
        node_rev.append(cur.node)
    node_rev.reverse()
    edge_rev.reverse()
    return node_rev, edge_rev


def verify_path(
    graph: Graph,
    nodes: list[int],
    edges: list[int],
    source: int,
    target: int,
) -> tuple[float, float]:
    """校验重建结果：首尾、边存在性、节点与边的衔接，并累加权重。

    返回该路径的 (time, cost)。任何不一致抛出 AssertionError。
    """
    assert nodes[0] == source, "路径起点不是 source"
    assert nodes[-1] == target, "路径终点不是 target"
    assert len(edges) == len(nodes) - 1, "边数与节点数不匹配"
    t = c = 0.0
    for k, e in enumerate(edges):
        assert int(graph.edge_u[e]) == nodes[k], f"第 {k} 条边尾端不匹配"
        assert int(graph.edge_v[e]) == nodes[k + 1], f"第 {k} 条边首端不匹配"
        t += float(graph.time[e])
        c += float(graph.cost[e])
    if len(nodes) == 1:
        assert source == target, "长度 0 的路径只允许 source == target"
    return t, c
