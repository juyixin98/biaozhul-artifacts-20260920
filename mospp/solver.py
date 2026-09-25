"""双目标（时间、费用）最短路标签算法。

算法概述
========
对每个顶点 v 维护一个 **非支配标签集** L(v)。标签
``(time, cost, visited, pred_label, arc)`` 表示从源点到 v 的一条简单路径：

* time/cost —— 该路径上两维权重的累计和（非负）；
* visited   —— 路径已访问顶点的位掩码（保证 **简单路径**，即顶点不重复）；
* pred_label / arc —— 前驱标签和最后一条弧，用于事后 **路径重建**。

扩展规则：从堆中取出当前最小序标签 (time, cost, label_id)（广义 Dijkstra
顺序，时间优先），沿每条出弧 (v,w) 扩展。因为权重非负，扩展只会增大两维。
若 w 已在 visited 中（会形成环）则跳过——这同时也保证了零权环不会让算法
不终止（含零权环的绕行若产生相同权重、更少访问顶点的标签会被正常保留，
但任何绕行都不会成环后继续无限扩展）。

支配判定（同一顶点的两个标签 a、b）
-----------------------------------
* 若 (t_a,c_a) 在容差内 **严格支配** (t_b,c_b)（一维严格小、另一维不大），
  则直接淘汰 b。这与各自经过哪些顶点无关：同顶点标签拥有相同的出边集合，
  权重严格更优的标签，其任意非负延伸都严格更优。
* 若两维在容差内 **相等**：边序列身份相同（同一路径）视为重复，只保留一个；
  否则作为不同走法的“相等标签” **都保留**——即使它们访问的顶点集相同（不同
  排列也是不同路线）。这里不做 “visited 子集剪枝”，因为本实现要枚举全部不同
  的最优简单路径：超集标签可能经由子集标签不能走的边形成一条同权但不同走法的
  Pareto 路径（多个零权路径即如此）。

预算剪枝
--------
可给出 ``time_budget`` / ``cost_budget``：扩展后任一维超预算的标签直接不入集，
并计入 ``stats.pruned_by_budget``。

标签上限
--------
``label_cap`` 限制求解过程中创建的标签总数（含源点标签）。达到上限时算法
停止，返回当前已找到的全部目标标签，状态置为 ``truncated`` 并报告
``stats.label_cap`` 与 ``stats.truncated=true``——此时 Pareto 前沿 **可能不完整**。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Optional

from .errors import TruncationError
from .tolerance import DEFAULT_EPS, weak_le, strict_lt


@dataclass(frozen=True)
class SolveLimits:
    """小/中规模范围（输入校验的硬边界）。"""

    max_nodes: int = 2000
    max_edges: int = 10000
    max_label_cap: int = 2_000_000
    default_label_cap: int = 200_000
    max_weight: float = 1.0e9


@dataclass
class Label:
    """一个非支配路径标签。"""

    time: float
    cost: float
    node: int
    visited: int                 # 已访问顶点位掩码
    pred: Optional["Label"] = None
    arc: object = None           # 最后一条 Arc（重建用）
    label_id: int = 0
    dead: bool = False           # 被后到的更强标签支配后置 True

    def path(self):
        """重建顶点内部下标序列（含源点、终点）。"""
        rev_nodes = []
        rev_arcs = []
        lab = self
        while lab is not None:
            rev_nodes.append(lab.node)
            if lab.arc is not None:
                rev_arcs.append(lab.arc)
            lab = lab.pred
        return list(reversed(rev_nodes)), list(reversed(rev_arcs))

    def node_tuple(self):
        """路径的顶点序列元组（内部下标），作为路径身份。"""
        rev = []
        lab = self
        while lab is not None:
            rev.append(lab.node)
            lab = lab.pred
        return tuple(reversed(rev))

    def same_path(self, other: "Label") -> bool:
        """两个标签是否代表同一条 **顶点级** 简单路径。

        路径身份定义为 **顶点序列**（与“简单路径”的标准定义、以及交叉验证用的
        枚举器一致）：

        * 顶点序列相同即视为同一路径，哪怕是经由两条端点相同的 **平行边** 到达
          （平行边在顶点序列层面不可区分；默认只保留一条，避免解因平行边膨胀，
          具体走的弧可通过响应 ``edges`` 中的 ``key`` 辨识）；
        * 顶点集合相同但 **顺序不同**（0-2-1-3 与 0-2-3-1）是不同路径。
        """
        a, b = self, other
        while a is not None and b is not None:
            if a.node != b.node:
                return False
            a, b = a.pred, b.pred
        return a is None and b is None


@dataclass
class SolveResult:
    status: str
    target_labels: list[Label] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


class _DomBucket:
    """单个顶点的非支配标签集，封装容差支配判定。

    标签规模在中小图上通常可控，采用 O(k) 线性比较；这也是标签类算法的常见
    实现方式（k 为该顶点的非支配标签数）。
    """

    __slots__ = ("labels", "eps")

    def __init__(self, eps: float):
        self.labels: list[Label] = []
        self.eps = eps

    def try_add(self, cand: Label):
        """尝试把 cand 放入集合。

        :returns: (accepted, killed)。accepted=True 表示被接受（可能顺带把旧
                  标签标记为 dead 并移除，数量为 killed）；False 表示因被支配
                  或重复而拒绝。

        权重关系按 **对称** 方式三分类（避免容差带边界处把“近似相等”误判成
        严格支配）：

        * 两方向都 weak、都不 strict ⇒ 容差内 **相等**；
        * 仅一个方向 weak（该方向可能 strict 或也在带内）⇒ 该方向 **不差于**，
          其中至少一维带外严格小才算 **支配**，否则仍按“相等”处理；
        * 两方向都不 weak ⇒ 互不支配。
        """
        eps = self.eps
        survivors: list[Label] = []
        killed = 0
        for old in self.labels:
            if old.dead:
                continue
            cand_weak = weak_le(
                cand.time, cand.cost, old.time, old.cost, eps
            )
            cand_strict = strict_lt(
                cand.time, cand.cost, old.time, old.cost, eps
            )
            old_weak = weak_le(
                old.time, old.cost, cand.time, cand.cost, eps
            )
            old_strict = strict_lt(
                old.time, old.cost, cand.time, cand.cost, eps
            )

            # 对称相等：两个方向都没有“超出容差带”的维度。
            weight_eq = not cand_strict and not old_strict

            if weight_eq:
                # 权重在容差内相等：
                #  - 是同一条路径（边序列身份相同）⇒ 完全重复，丢弃 cand；
                #  - 否则（不同走法，哪怕 visited 顶点集恰好相同）⇒ 都保留。
                #
                # 这里刻意 **不** 用 “visited(a)⊆visited(b) 就删 b” 的扩展空间
                # 剪枝，也不仅凭 visited 相同判重：本实现要枚举全部不同的最优
                # 简单路径。同顶点集的不同排列（0-2-1-3 与 0-2-3-1）是不同路线，
                # 超集标签还可能经子集标签不能走的边形成新的同权 Pareto 路径。
                if cand.same_path(old):
                    return False, 0
                survivors.append(old)
                continue

            # 严格权重支配与 visited 无关：同顶点的两个标签拥有完全相同的
            # 出边集合，权重严格更优（一维严格小、另一维不大）的标签，其任意
            # 非负延伸都严格更优，被支配标签可安全删除（无论各自经过哪些点）。
            if old_weak and old_strict:
                return False, 0  # 旧标签严格支配 cand
            if cand_weak and cand_strict:
                old.dead = True  # cand 严格支配旧标签
                killed += 1
                continue
            survivors.append(old)

        survivors.append(cand)
        self.labels = survivors
        return True, killed


class Solver:
    """双目标最短路求解器。

    :param graph: :class:`~mospp.graph.Graph`（已通过 from_request 校验）。
    :param eps: 容差参数，见 :mod:`mospp.tolerance`。
    """

    def __init__(self, graph, eps: float = DEFAULT_EPS):
        self.g = graph
        self.eps = eps
        self.buckets: list[_DomBucket] = [
            _DomBucket(eps) for _ in range(graph.n)
        ]
        self.stats = {
            "labels_created": 0,      # 创建的标签总数（含源标签、被拒标签）
            "labels_accepted": 0,     # 通过同点支配判定并入集的标签数
            "labels_rejected": 0,     # 因被支配或重复被拒的标签数
            "old_labels_killed": 0,   # 入集时顺带淘汰的旧标签数
            "labels_cycle_skipped": 0,  # 因回到已访问顶点（成环）跳过的扩展数
            "pruned_by_budget": 0,    # 因超出预算被剪枝的扩展数
            "heap_pops": 0,
            "dead_pops": 0,
            "truncated": False,
        }

    # ---- 内部 ----
    def _new_label(self, node, time, cost, visited, pred, arc) -> Label:
        lab = Label(
            time=float(time),
            cost=float(cost),
            node=node,
            visited=visited,
            pred=pred,
            arc=arc,
            label_id=self.stats["labels_created"],
        )
        self.stats["labels_created"] += 1
        return lab

    def _within_budgets(self, t, c, time_budget, cost_budget) -> bool:
        eps = self.eps
        scale = max(1.0, abs(t), abs(c))
        tol = eps * scale
        if time_budget is not None and t > float(time_budget) + tol:
            return False
        if cost_budget is not None and c > float(cost_budget) + tol:
            return False
        return True

    def _add_or_reject(self, lab, cap: int) -> bool:
        """把新标签交给桶判定；统计接受/拒绝/淘汰数，执行标签上限。

        :returns: 桶是否接受该标签。
        """
        if self.stats["labels_created"] > cap:
            self.stats["truncated"] = True
            raise TruncationError()
        accepted, killed = self.buckets[lab.node].try_add(lab)
        if accepted:
            self.stats["labels_accepted"] += 1
            self.stats["old_labels_killed"] += killed
        else:
            self.stats["labels_rejected"] += 1
        return accepted

    # ---- 主流程 ----
    def solve(
        self,
        source_idx: int,
        target_idx: int,
        time_budget: Optional[float] = None,
        cost_budget: Optional[float] = None,
        label_cap: Optional[int] = None,
    ) -> SolveResult:
        cap = int(label_cap) if label_cap is not None else SolveLimits.default_label_cap

        # 弧权重的 NumPy 规整存储（freeze 时构建）；扩展时按下标取出。
        arc_time = self.g._arc_time
        arc_cost = self.g._arc_cost

        heap: list = []
        src_label = self._new_label(
            source_idx, 0.0, 0.0, 1 << source_idx, None, None
        )
        # 源标签不经过预算检查（零权重恒满足非负预算；预算为负由 API 层拒绝）。
        self.buckets[source_idx].try_add(src_label)
        heapq.heappush(heap, (0.0, 0.0, src_label.label_id, src_label))

        truncated = False
        try:
            while heap:
                t, c, _lid, lab = heapq.heappop(heap)
                self.stats["heap_pops"] += 1
                if lab.dead:
                    self.stats["dead_pops"] += 1
                    continue
                v = lab.node
                if v == target_idx:
                    # 目标标签不扩展：非负权下任何延伸只会更差。
                    continue
                for arc in self.g.outgoing(v):
                    w = arc.v
                    if lab.visited & (1 << w):
                        self.stats["labels_cycle_skipped"] += 1
                        continue
                    ai = arc.idx
                    nt = t + float(arc_time[ai])
                    nc = c + float(arc_cost[ai])
                    if not self._within_budgets(
                        nt, nc, time_budget, cost_budget
                    ):
                        self.stats["pruned_by_budget"] += 1
                        continue
                    nlab = self._new_label(
                        w,
                        nt,
                        nc,
                        lab.visited | (1 << w),
                        lab,
                        arc,
                    )
                    accepted = self._add_or_reject(nlab, cap)
                    if accepted:
                        heapq.heappush(heap, (nt, nc, nlab.label_id, nlab))
        except TruncationError:
            truncated = True

        target = [
            lab for lab in self.buckets[target_idx].labels if not lab.dead
        ]
        # 终点标签都是“终止路径”，不再延伸，前缀差异不再重要——因此最后按
        # 纯权重再做一次 Pareto 过滤：过程中两个 visited 互不包含、无法互相
        # 淘汰的标签，到了终点可能一个严格支配另一个。权重在容差内相等的
        # 不同路径都保留（“相等标签”）。
        target = self._terminal_filter(target)
        target.sort(key=lambda x: (x.time, x.cost, x.label_id))

        status_out = "truncated" if truncated else (
            "unreachable" if not target else "ok"
        )
        return SolveResult(status=status_out, target_labels=target, stats=self.stats)

    def _terminal_filter(self, labels):
        """终点收尾：仅剔除 **严格被另一条路径权重支配** 的标签。

        权重在容差内相等的不同简单路径（visited 不同）一律保留——它们是不同
        的相等标签，调用方可能需要全部路线（例如同样时间/费用的不同走法）。
        """
        eps = self.eps
        kept = []
        for cand in labels:
            dominated = False
            for other in labels:
                if other is cand:
                    continue
                if weak_le(
                    other.time, other.cost, cand.time, cand.cost, eps
                ) and strict_lt(
                    other.time, other.cost, cand.time, cand.cost, eps
                ):
                    dominated = True
                    break
            if not dominated:
                kept.append(cand)
        return kept
