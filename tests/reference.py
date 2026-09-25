"""参考实现：DFS 枚举全部简单 s-t 路径（仅用于小图交叉验证）。

这是 **故意写得朴素** 的独立实现，与求解器不共享任何代码路径，
用于在小图上穷举全部简单路径并计算 Pareto 前沿作对照。
"""

from __future__ import annotations

from mospp.tolerance import weak_le, strict_lt, weights_equal, DEFAULT_EPS


def enumerate_simple_paths(graph, s: int, t: int):
    """枚举从 s 到 t 的全部简单路径。

    :yields: (node_tuple, total_time, total_cost)
    """
    adj = graph.outgoing

    def dfs(v, visited, nodes, tt, tc):
        if v == t:
            yield tuple(nodes), tt, tc
            return
        for arc in adj(v):
            w = arc.v
            mask = 1 << w
            if visited & mask:
                continue
            nodes.append(w)
            yield from dfs(w, visited | mask, nodes, tt + arc.time, tc + arc.cost)
            nodes.pop()

    yield from dfs(s, 1 << s, [s], 0.0, 0.0)


def reference_pareto(graph, s: int, t: int, eps: float = DEFAULT_EPS):
    """枚举 + 朴素 O(k^2) Pareto 过滤。

    :returns: 列表 [(node_tuple, time, cost), ...]，按 (time, cost) 排序。
    """
    paths = list(enumerate_simple_paths(graph, s, t))
    keep = []
    for p in paths:
        _, t1, c1 = p
        dominated = False
        for q in paths:
            if p is q:
                continue
            _, t2, c2 = q
            if weak_le(t2, c2, t1, c1, eps) and strict_lt(t2, c2, t1, c1, eps):
                dominated = True
                break
        if not dominated:
            keep.append(p)
    keep.sort(key=lambda x: (x[1], x[2], x[0]))
    return keep


def fronts_equal_with_tol(ref, got, eps: float = DEFAULT_EPS):
    """两个 Pareto 前沿（(time,cost) 对序列）是否在容差下重合。

    采用一一配对：把权重对看作无序多重集，按容差贪心匹配。
    """
    used = [False] * len(ref)
    if len(ref) != len(got):
        return False
    for gt, gc in got:
        hit = -1
        for j, (_, rt, rc) in enumerate(ref):
            if not used[j] and weights_equal(gt, gc, rt, rc, eps):
                hit = j
                break
        if hit < 0:
            return False
        used[hit] = True
    return all(used)
