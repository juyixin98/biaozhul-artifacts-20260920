"""测试辅助：可行流穷举器（独立、朴素的参考实现）。

思路
----
递归地在 **当前残量网络** 中枚举简单 s-t 有向路径（DFS，顶点不重复），
对每条路径发送 1 个单位（容量按 1 单位逐次增广），在搜索树每个节点处
记录 ``(当前流量, 当前费用)``；取每个流量值下的最小费用，即为各可行
整数值流量的最小费用。

正确性依据：任意值为 k 的整数可行流都可分解为 k 条单位 s-t 路
（外加若干环；最优流不含负费用环，去环不会增加费用），因此逐单位沿
残量网络中某条简单 s-t 路增广的搜索树必然覆盖最优流。

该实现刻意写得朴素、与被测算法完全独立（纯 Python 字典/集合，
不用 NumPy），仅适合 n ≤ 6、容量 ≤ 2 的小图——正是随机穷举测试的规模。
对“残量状态 → 到达该状态的最小费用”记忆化，避免指数级重复搜索。
"""

from __future__ import annotations

from collections.abc import Callable

# residual[(u, arc)] = 残量容量；arc 偶数为前向弧，奇数为反向弧（arc ^ 1 配对）
Residual = dict[tuple[int, int], int]
Adj = dict[int, list[tuple[int, int]]]


def build_residual(
    n: int,
    edges: list[tuple[int, int, int, int]],
) -> tuple[Residual, Adj]:
    """按成对弧方式构造残量网络（与生产实现独立的朴素版本）。"""
    res: Residual = {}
    adj: Adj = {u: [] for u in range(n)}
    for i, (u, v, cap, _cost) in enumerate(edges):
        fwd, rev = 2 * i, 2 * i + 1
        res[(u, fwd)] = cap
        res[(v, rev)] = 0
        adj[u].append((v, fwd))
        adj[v].append((u, rev))
    return res, adj


def enumerate_min_cost(
    n: int,
    source: int,
    sink: int,
    edges: list[tuple[int, int, int, int]],
    max_augment_steps: int = 14,
) -> dict[int, int]:
    """返回 ``{可行流量: 该流量下的最小费用}``（流量 0 费用 0 总在其中）。"""
    res, adj = build_residual(n, edges)
    arc_cost = {2 * i: c for i, (_u, _v, _cap, c) in enumerate(edges)}
    arc_cost |= {2 * i + 1: -c for i, (_u, _v, _cap, c) in enumerate(edges)}

    best: dict[int, int] = {0: 0}
    # 记忆化：前向弧残量容量元组 -> 到达该状态的最小费用。
    seen: dict[tuple[int, ...], int] = {}
    m = len(edges)

    def state_key() -> tuple[int, ...]:
        return tuple(res[(edges[i][0], 2 * i)] for i in range(m))

    def dfs(depth_left: int, flow: int, cost: int) -> None:
        key = state_key()
        prior = seen.get(key)
        if prior is not None and prior <= cost:
            return  # 同一残量状态曾以不更高费用到达，无需重复展开
        seen[key] = cost

        if depth_left <= 0:
            return

        path_arcs: list[int] = []

        def extend(u: int, visited: frozenset[int]) -> None:
            if u == sink:
                path_cost = sum(arc_cost[a] for a in path_arcs)
                for a in path_arcs:
                    res[(a_head(a), a)] -= 1
                    res[(a_tail(a), a ^ 1)] += 1
                new_cost = cost + path_cost
                best[flow + 1] = min(best.get(flow + 1, new_cost), new_cost)
                dfs(depth_left - 1, flow + 1, new_cost)
                for a in reversed(path_arcs):
                    res[(a_tail(a), a ^ 1)] -= 1
                    res[(a_head(a), a)] += 1
                return
            for v, arc in adj[u]:
                if res[(u, arc)] > 0 and v not in visited:
                    path_arcs.append(arc)
                    extend(v, visited | {v})
                    path_arcs.pop()

        def a_head(a: int) -> int:
            i = a // 2
            return edges[i][0] if a % 2 == 0 else edges[i][1]

        def a_tail(a: int) -> int:
            i = a // 2
            return edges[i][1] if a % 2 == 0 else edges[i][0]

        extend(source, frozenset({source}))

    dfs(max_augment_steps, 0, 0)
    return best


def has_reachable_negative_cycle(
    n: int,
    source: int,
    edges: list[tuple[int, int, int, int]],
) -> bool:
    """Bellman–Ford：初始残量网络中是否存在源点可达负环（独立参考实现）。"""
    arcs: list[tuple[int, int, int]] = [
        (u, v, c) for u, v, cap, c in edges if cap > 0
    ]
    inf = 10**18
    dist = [inf] * n
    dist[source] = 0
    for _ in range(n - 1):
        changed = False
        for u, v, c in arcs:
            if dist[u] < inf and dist[u] + c < dist[v]:
                dist[v] = dist[u] + c
                changed = True
        if not changed:
            return False
    for u, v, c in arcs:
        if dist[u] < inf and dist[u] + c < dist[v]:
            return True
    return False


def random_edges(
    rng,
    n: int,
    m: int,
    max_cap: int = 2,
    cost_range: int = 3,
    zero_cap_fraction: float = 0.15,
) -> list[tuple[int, int, int, int]]:
    """生成随机平行边允许的整数小图（含零容量边与可能的自环）。"""
    edges: list[tuple[int, int, int, int]] = []
    for _ in range(m):
        u = int(rng.integers(0, n))
        v = int(rng.integers(0, n))
        if rng.random() < zero_cap_fraction:
            cap = 0
        else:
            cap = int(rng.integers(0, max_cap + 1))
        cost = int(rng.integers(-cost_range, cost_range + 1))
        edges.append((u, v, cap, cost))
    return edges


def make_solver_fn() -> Callable:
    """返回用生产代码按指定 required_flow 求解的小闭包。"""
    from mcf.graph import FlowNetwork
    from mcf.solver import min_cost_max_flow

    def solve(n, s, t, edges, required_flow=None):
        net = FlowNetwork(n, s, t, edges)
        return min_cost_max_flow(net, required_flow)

    return solve
