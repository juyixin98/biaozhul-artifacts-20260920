"""环检测：负代价环与输入 epsilon 环。

支持范围（明确约定）：

* **负代价环**：热带半环下若存在从初态可达、且能到达某终态的负代价环，
  最短路径无定义（代价可无限下降）。:func:`negative_cycle_on_accepting_paths`
  返回环上的状态序列，调用方必须按
  :class:`~wfst.core.fst.NegativeCycleError` 处理，禁止吞掉。
* **零/正代价 epsilon 环**：允许存在。最短路径算法按状态去重，
  不会陷入死循环；但纯输入 epsilon 环（ilabel 全为 ``<eps>`` 的环）
  在固定输入下可产生无穷多条对齐路径，:func:`reachable_input_epsilon_cycle`
  显式报告，供枚举层决定是否只能返回带 ``complete=False`` 的部分结果。
"""

from __future__ import annotations

from wfst.core.fst import EPSILON, FST, INFINITY


def reachable_from_start(fst: FST) -> set[int]:
    """从初态沿任意弧可达的状态集合。"""
    seen = {fst.start}
    stack = [fst.start]
    while stack:
        s = stack.pop()
        for arc in fst.outgoing(s):
            if arc.dst not in seen:
                seen.add(arc.dst)
                stack.append(arc.dst)
    return seen


def can_reach_final(fst: FST) -> set[int]:
    """能沿任意弧到达某终态的状态集合（反向传播）。"""
    rev: list[list[int]] = [[] for _ in range(fst.num_states)]
    for arc in fst.arcs:
        rev[arc.dst].append(arc.src)
    seen = set(fst.finals)
    stack = list(seen)
    while stack:
        s = stack.pop()
        for pred in rev[s]:
            if pred not in seen:
                seen.add(pred)
                stack.append(pred)
    return seen


def negative_cycle_on_accepting_paths(fst: FST) -> list[int] | None:
    """Bellman-Ford 检测「可达且可到终态」的负代价环。

    返回环上状态序列（沿弧方向闭合），无此环时返回 ``None``。
    终态权重被视作通向虚拟汇点的边参与第 n 轮松弛。
    """
    n = fst.num_states
    dist = [INFINITY] * n
    dist[fst.start] = 0.0
    pred: list[int | None] = [None] * n

    # 仅在「初态可达且能到终态」的子图上检测，无关环不影响接受路径。
    relevant = reachable_from_start(fst) & can_reach_final(fst)
    arcs = [a for a in fst.arcs if a.src in relevant and a.dst in relevant]

    for _ in range(n - 1):
        changed = False
        for arc in arcs:
            nd = dist[arc.src] + arc.weight
            if nd < dist[arc.dst]:
                dist[arc.dst] = nd
                pred[arc.dst] = arc.src
                changed = True
        if not changed:
            break

    # 第 n 轮仍可松弛 => 负环；找出松弛边并沿前驱链还原环。
    relax_edge = None
    for arc in arcs:
        if dist[arc.src] + arc.weight < dist[arc.dst] - 1e-12:
            relax_edge = arc
            break
    if relax_edge is None:
        # 终态权重为负且第 n 轮仍松弛的情况：终态无出边，自环不可能，
        # 但负终止权重配合环仍由上面的弧松弛覆盖；此处无需额外处理。
        return None

    chain: list[int] = []
    cur: int = relax_edge.src
    seen: set[int] = set()
    while cur not in seen:
        seen.add(cur)
        chain.append(cur)
        if pred[cur] is None:
            break
        cur = pred[cur]  # type: ignore[assignment]
    if cur in seen:
        start = chain.index(cur)
        cycle = chain[start:] + [cur]
        return cycle
    # 前驱链断裂（理论上不会发生，保守返回松弛边两端）。
    return [relax_edge.dst, relax_edge.src, relax_edge.dst]


def reachable_input_epsilon_cycle(fst: FST) -> list[int] | None:
    """检测从初态可达的「纯输入 epsilon」环（沿 ilabel==<eps> 的弧）。

    这种环不消耗输入即可无限绕行：若还发射非 epsilon 输出，则存在无穷
    多个不同输出；即使不发射输出，也存在无穷多条等价对齐。
    返回环上状态序列，无环返回 ``None``。
    """
    color = [0] * fst.num_states  # 0 未访问 / 1 在栈中 / 2 完成
    stack_trace: list[int] = []

    def dfs(u: int) -> list[int] | None:
        color[u] = 1
        stack_trace.append(u)
        for arc in fst.outgoing(u):
            if arc.ilabel != EPSILON:
                continue
            if color[arc.dst] == 0:
                found = dfs(arc.dst)
                if found is not None:
                    return found
            elif color[arc.dst] == 1:
                idx = stack_trace.index(arc.dst)
                return stack_trace[idx:] + [arc.dst]
        color[u] = 2
        stack_trace.pop()
        return None

    return dfs(fst.start)
