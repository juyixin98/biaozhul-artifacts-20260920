"""接受路径的 k-最短枚举（n-best，热带半环）。

输入已由上游固定（字符链接受机与转换器组合的结果），本模块在一个
FST 上枚举从初态到终态的路径，按发射的**输出符号串**去重，返回
``(代价, 输出串)``。

排序与确定性
------------
* 主键总代价升序；代价相同按输出串的稳定字典序（Python Unicode 码位序）。
* best-first 搜索配合非负调整权重，同一输出串的**第一次汇点弹出**即
  最小代价；其后更贵的重复对齐被优势剪枝丢弃，零代价 epsilon 自环也
  不会造成无限循环。

负代价的支持范围（明确约定）
----------------------------
* 允许负权弧：先用 Bellman-Ford 计算每个状态到虚拟汇点的最短距离作为
  Johnson 势函数，把边权重标为非负后再 best-first，报告的仍是原始代价。
* 若存在「初态可达且能到终态」的负代价环，最短路径无定义，抛出
  :class:`NegativeCycleError`，绝不返回看似成功的结果。

预算与完成标志
--------------
``budget`` 限制优先队列弹出次数（= 搜索节点展开数）。仅当预算耗尽而
尚有未展开节点时置 ``complete=False``（未完成）；队列自然清空或已找满
``k`` 条均为 ``complete=True``。纯输入 epsilon 发射环可产生无穷多个输出
时，预算是唯一的终止保障，调用方通过 ``complete=False`` 得知结果只是
前缀。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass

from wfst.core.cycles import can_reach_final, negative_cycle_on_accepting_paths
from wfst.core.fst import EPSILON, FST, AlignmentError, NegativeCycleError

_EPS = 1e-12


@dataclass(frozen=True, slots=True)
class Hypothesis:
    """一条候选输出。"""

    output: str
    cost: float

    def as_dict(self) -> dict:
        return {"output": self.output, "cost": round(self.cost, 6)}


@dataclass(slots=True)
class NBestResult:
    """n-best 枚举结果。

    ``complete=False`` 仅在预算耗尽（未完成）时出现；找满 k 条或队列
    自然穷尽均为 ``complete=True``。
    """

    hypotheses: list[Hypothesis]
    complete: bool
    pops: int
    budget: int

    @property
    def exhausted(self) -> bool:
        """预算是否耗尽（未完成）。"""
        return not self.complete


def _sink_potentials(fst: FST) -> list[float]:
    """每个状态到「虚拟汇点」的最短距离 h(s)（终态以终止权重连汇点）。

    在反向图上以汇点为源做 Bellman-Ford。调用方已保证无负环；到不了
    汇点的状态保持 inf（这些状态不可能位于接受路径上，展开时剪枝）。
    """
    n = fst.num_states
    sink = n
    h = [float("inf")] * (n + 1)
    h[sink] = 0.0
    rev: list[list[tuple[int, float]]] = [[] for _ in range(n + 1)]
    for arc in fst.arcs:
        rev[arc.dst].append((arc.src, arc.weight))
    for s, w in fst.finals.items():
        rev[sink].append((s, w))

    for _ in range(n + 1):
        changed = False
        for u in range(n + 1):
            if h[u] == float("inf"):
                continue
            base = h[u]
            for v, w in rev[u]:
                nd = base + w
                if nd < h[v]:
                    h[v] = nd
                    changed = True
        if not changed:
            break
    return h


def _has_negative_weight(fst: FST) -> bool:
    return any(a.weight < 0.0 for a in fst.arcs) or any(
        w < 0.0 for w in fst.finals.values()
    )


def nbest_paths(fst: FST, k: int = 5, budget: int = 200_000) -> NBestResult:
    """枚举至多 ``k`` 条**不同输出串**的最优假设。

    :raises ValueError: ``k`` 非正。
    :raises NegativeCycleError: 接受路径上存在负代价环。
    :raises AlignmentError: 输入不被接受（无任何接受路径）。
    """
    if k <= 0:
        raise ValueError("k 必须为正整数")

    neg_cycle = negative_cycle_on_accepting_paths(fst)
    if neg_cycle is not None:
        raise NegativeCycleError(neg_cycle)

    # 到不了终态的状态一律剪枝（负权时 h==inf 亦覆盖）。
    live = can_reach_final(fst)

    if _has_negative_weight(fst):
        h = _sink_potentials(fst)
    else:
        h = [0.0] * (fst.num_states + 1)

    # 虚拟汇点：「在终态结束」建模为一条代价为终止权重的 epsilon 弧。
    # 显式入堆后，堆键单调性覆盖完整路径代价，保证同一输出串第一次在
    # 汇点弹出时即全局最小代价（否则更贵的终止方式可能先被记录）。
    sink = fst.num_states

    def hv(s: int) -> float:
        return 0.0 if h[s] == float("inf") else h[s]

    # 堆键 = 原前缀代价 + h(状态)（省略常数偏移 -h(start)，不改变顺序）。
    # 重标度边权 w + h(v) - h(u) >= 0，终止弧 fw + 0 - h(s) >= 0，
    # 故堆键沿任意路径单调不减，best-first 的第一条汇点路径即最优。
    counter = 0
    heap: list[tuple[float, str, float, int, int]] = []

    def push(state: int, output: str, orig_cost: float) -> None:
        nonlocal counter
        counter += 1
        heapq.heappush(
            heap, (orig_cost + hv(state), output, orig_cost, state, counter)
        )

    push(fst.start, "", 0.0)

    # (状态, 输出串) -> 已弹出的最小堆键。优势去重：非负调整权重下，
    # 后到的同状态同输出代价不会更低，其后缀展开完全相同，直接剪枝。
    settled: dict[tuple[int, str], float] = {}
    found: dict[str, float] = {}
    pops = 0
    stopped_by_budget = False

    while heap:
        key, output, orig_cost, state, _tie = heapq.heappop(heap)
        pops += 1

        prev_key = settled.get((state, output))
        if prev_key is not None and key >= prev_key - _EPS:
            continue
        settled[(state, output)] = key
        over_budget = pops >= budget

        if state == sink:
            # 首次汇点弹出即该输出串的最优代价。汇点无出边，记录后
            # 继续搜索其余输出串，直到找满 k 条或预算耗尽。
            found[output] = orig_cost
            if len(found) >= k:
                break
            if over_budget:
                stopped_by_budget = True
                break
            continue

        if over_budget:
            stopped_by_budget = True
            break

        # 终止弧：终态 -> 汇点（epsilon，代价 = 终止权重）。
        if state in fst.finals:
            push(sink, output, orig_cost + fst.finals[state])

        for arc in fst.outgoing(state):
            if arc.dst not in live:
                continue  # 不可能到达终态
            emitted = "" if arc.olabel == EPSILON else arc.olabel
            push(arc.dst, output + emitted, orig_cost + arc.weight)

    if not found:
        raise AlignmentError("输入不被接受：不存在从初态到终态的路径")

    complete = not stopped_by_budget
    hyps = sorted(
        (Hypothesis(output=o, cost=c) for o, c in found.items()),
        key=lambda hyp: (hyp.cost, hyp.output),
    )
    return NBestResult(
        hypotheses=hyps, complete=complete, pops=pops, budget=budget
    )
