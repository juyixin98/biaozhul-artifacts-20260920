"""激活检查点规划器。

方案表示
--------
:class:`Plan` 用拓扑序上的**块边界位置**描述分块：``boundary_positions``
中的每个位置 ``p`` 表示在第 ``p`` 个节点之后切块。除最后一块外的每个块
都是"检查点块"——前向只保留块末激活 + RNG 快照，反向整块重放。

方案合法性（:meth:`Plan.validate`）
----------------------------------
1. 边界位置互不重复且在 ``[0, n-2]`` 内（不允许空尾块）；
2. **跨块边只能源自块边界节点**：若边 u->v 中 u、v 不在同一块，u 必须是
   其所在块的块末节点。否则重放时 v 依赖的激活无法恢复（这正是
   "删掉必要激活后反向报错"的静态拦截）；
3. 图输出位于最后一块，或本身就是边界节点（输出值必须可保留）。

规划策略
--------
小图上对全部 ``2^(n-1)`` 种切分做穷举（分支案例规模下完全够用），
用 :mod:`recomp_scheduler.memory` 的静态模拟器计算每个合法方案的预测峰值
与重算成本，在预算内选择**重算量最小**的方案（平局依次比较峰值、块数）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Iterator

from .errors import BudgetInfeasibleError, InvalidPlanError
from .graph import Graph
from .memory import MemoryProfile, simulate


@dataclass(frozen=True)
class Plan:
    boundary_positions: tuple[int, ...]
    n_nodes: int
    block_count: int = field(init=False)
    blocks: tuple[tuple[int, int], ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        bps = tuple(sorted(self.boundary_positions))
        if len(set(bps)) != len(bps):
            raise InvalidPlanError(
                "boundary positions must be unique",
                boundary_positions=list(self.boundary_positions),
            )
        if self.n_nodes <= 0:
            raise InvalidPlanError("plan requires at least one node", n_nodes=self.n_nodes)
        if any(p < 0 or p >= self.n_nodes - 1 for p in bps):
            raise InvalidPlanError(
                "boundary position out of range",
                boundary_positions=list(bps),
                n_nodes=self.n_nodes,
            )
        spans: list[tuple[int, int]] = []
        start = 0
        for p in bps:
            spans.append((start, p))
            start = p + 1
        spans.append((start, self.n_nodes - 1))
        object.__setattr__(self, "boundary_positions", bps)
        object.__setattr__(self, "block_count", len(spans))
        object.__setattr__(self, "blocks", tuple(spans))

    def block_index_of(self, pos: int) -> int:
        for b, (s, e) in enumerate(self.blocks):
            if s <= pos <= e:
                return b
        raise InvalidPlanError("position outside plan", pos=pos)

    def block_node_ids(self, graph: Graph) -> list[list[str]]:
        return [list(graph.order[s : e + 1]) for s, e in self.blocks]

    def validate(self, graph: Graph) -> None:
        """校验方案适用于给定图（抛 :class:`InvalidPlanError`）。"""
        n = len(graph.order)
        if n != self.n_nodes:
            raise InvalidPlanError(
                "plan was built for a different graph size",
                plan_nodes=self.n_nodes,
                graph_nodes=n,
            )
        pos = {v: i for i, v in enumerate(graph.order)}

        # 规则 2：跨块边必须源自块末节点。
        boundary_set = set(self.boundary_positions)
        for v in graph.order:
            node = graph.node(v)
            bv = self.block_index_of(pos[v])
            for u in node.inputs:
                bu = self.block_index_of(pos[u])
                if bu == bv:
                    continue
                if pos[u] not in boundary_set:
                    raise InvalidPlanError(
                        "cross-block edge does not originate at a block boundary; "
                        "required activation would be missing at recompute",
                        edge=f"{u}->{v}",
                        block_of_source=bu,
                        block_of_target=bv,
                    )

        # 规则 3：输出必须在最后一块或为边界节点。
        last_block = self.block_count - 1
        for o in graph.outputs:
            bo = self.block_index_of(pos[o])
            if bo != last_block and pos[o] not in boundary_set:
                raise InvalidPlanError(
                    "graph output is neither in last block nor a boundary node",
                    output=o,
                    block=bo,
                )

    def to_dict(self, graph: Graph) -> dict[str, Any]:
        return {
            "boundary_positions": list(self.boundary_positions),
            "block_count": self.block_count,
            "blocks": [
                {"index": b, "nodes": graph.order[s : e + 1]}
                for b, (s, e) in enumerate(self.blocks)
            ],
        }


def make_plan(boundary_positions: tuple[int, ...], n_nodes: int) -> Plan:
    return Plan(boundary_positions=tuple(boundary_positions), n_nodes=n_nodes)


# --------------------------------------------------------------------- #
# 穷举
# --------------------------------------------------------------------- #
def iter_all_plans(graph: Graph) -> Iterator[Plan]:
    """按切分数量递增枚举全部（含不检查点的基线）方案。"""
    n = len(graph.order)
    yield make_plan((), n)
    for k in range(1, n):
        for cuts in combinations(range(n - 1), k):
            yield make_plan(cuts, n)


def legal_plans(graph: Graph) -> list[tuple[Plan, MemoryProfile]]:
    """返回全部合法方案及其静态画像（按块数、边界字典序排序）。"""
    out: list[tuple[Plan, MemoryProfile]] = []
    for plan in iter_all_plans(graph):
        try:
            plan.validate(graph)
        except InvalidPlanError:
            continue
        out.append((plan, simulate(graph, plan)))
    out.sort(key=lambda pair: (pair[0].block_count, pair[0].boundary_positions))
    return out


def baseline_plan(graph: Graph) -> tuple[Plan, MemoryProfile]:
    """不做任何检查点的基线方案（前向激活全部保留到反向）。"""
    plan = make_plan((), len(graph.order))
    return plan, simulate(graph, plan)


# --------------------------------------------------------------------- #
# 预算选择
# --------------------------------------------------------------------- #
@dataclass(frozen=True)
class PlannerChoice:
    plan: Plan
    profile: MemoryProfile
    budget_elements: int
    candidates_evaluated: int
    candidates_legal: int
    rejected_by_budget: int


def choose_plan(graph: Graph, budget_elements: int) -> PlannerChoice:
    """在预算内选择额外重算量最小的合法方案。

    预算不可行时抛 :class:`BudgetInfeasibleError`，details 中给出
    全局可达最小峰值与基线峰值，便于区分"预算过小"与"图本身放不下"。
    """
    if budget_elements <= 0:
        raise BudgetInfeasibleError(
            "budget must be positive", budget=int(budget_elements)
        )

    evaluated = 0
    legal = 0
    rejected = 0
    feasible: list[tuple[Plan, MemoryProfile]] = []
    min_peak: int | None = None

    for plan in iter_all_plans(graph):
        evaluated += 1
        try:
            plan.validate(graph)
        except InvalidPlanError:
            continue
        legal += 1
        profile = simulate(graph, plan)
        min_peak = profile.peak_elements if min_peak is None else min(min_peak, profile.peak_elements)
        if profile.peak_elements <= budget_elements:
            feasible.append((plan, profile))
        else:
            rejected += 1

    if not feasible:
        base_plan, base_profile = baseline_plan(graph)
        raise BudgetInfeasibleError(
            "no legal checkpoint plan fits the memory budget",
            phase="planning",
            budget=int(budget_elements),
            min_achievable_peak=int(min_peak or -1),
            baseline_peak=base_profile.peak_elements,
            legal_candidates=legal,
            reason=(
                "even maximal checkpointing exceeds the budget"
                if (min_peak is not None and min_peak > budget_elements)
                else "no legal plan exists"
            ),
        )

    def rank(item: tuple[Plan, MemoryProfile]):
        plan, profile = item
        return (
            profile.recompute_flops,     # 目标 1：额外计算量最小
            profile.peak_elements,       # 目标 2：峰值更低
            plan.block_count,            # 目标 3：分块更少（快照开销/复杂度）
            plan.boundary_positions,     # 确定性平局打破
        )

    best_plan, best_profile = min(feasible, key=rank)
    return PlannerChoice(
        plan=best_plan,
        profile=best_profile,
        budget_elements=int(budget_elements),
        candidates_evaluated=evaluated,
        candidates_legal=legal,
        rejected_by_budget=rejected,
    )
