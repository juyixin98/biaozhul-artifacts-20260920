"""不变量与规模测试：势函数约化费用非负、流守恒、中规模性能。"""

from __future__ import annotations

import time
import unittest

import numpy as np

from mcf import limits
from mcf.graph import FlowNetwork
from mcf.solver import min_cost_max_flow

from .brute_force import has_reachable_negative_cycle


def check_reduced_costs_nonnegative(net: FlowNetwork, potentials) -> None:
    """最终势函数下：源点可达残量弧的约化费用 c+p[u]-p[v] >= 0。"""
    # 可达集合（BFS，残量容量 > 0）
    reachable = [False] * net.n
    reachable[net.source] = True
    stack = [net.source]
    while stack:
        u = stack.pop()
        for arc in net.adj(u):
            if net.residual_capacity(arc) > 0:
                v = net.to_node(arc)
                if not reachable[v]:
                    reachable[v] = True
                    stack.append(v)
    for u in range(net.n):
        if not reachable[u]:
            continue
        for arc in net.adj(u):
            if net.residual_capacity(arc) <= 0:
                continue
            v = net.to_node(arc)
            assert potentials[u] is not None and potentials[v] is not None
            reduced = net.cost(arc) + potentials[u] - potentials[v]
            assert reduced >= 0, (
                f"残量弧 {u}->{v} 约化费用 {reduced} < 0，势函数不变量被破坏"
            )


class InvariantTests(unittest.TestCase):
    def test_potential_invariant_on_random_graphs(self) -> None:
        rng = np.random.default_rng(777)
        checked = 0
        attempts = 0
        while checked < 30 and attempts < 300:
            attempts += 1
            n = int(rng.integers(3, 12))
            s, t = 0, n - 1
            m = int(rng.integers(n, 3 * n))
            edges = []
            for _ in range(m):
                u = int(rng.integers(0, n))
                v = int(rng.integers(0, n))
                cap = int(rng.integers(0, 8))
                cost = int(rng.integers(-20, 21))
                edges.append((u, v, cap, cost))
            # 输入前提：不含源点可达负费用环
            if has_reachable_negative_cycle(n, s, edges):
                continue
            net = FlowNetwork(n, s, t, edges)
            result = min_cost_max_flow(net)
            check_reduced_costs_nonnegative(net, result.potentials)
            result.verify()
            checked += 1
        self.assertEqual(checked, 30)

    def test_huge_total_cost_uses_python_int(self) -> None:
        # 总费用 1e9 * 1e6 = 1e15 < int64；再用链式 10 条边累计 1e16，
        # 仍精确（Python int 累计费用，NumPy 只管距离/势函数）。
        edges = []
        n = 12
        for i in range(n - 1):
            edges.append((i, i + 1, 10**9, 10**6))
        result = min_cost_max_flow(FlowNetwork(n, 0, n - 1, edges))
        self.assertEqual(result.flow, 10**9)
        self.assertEqual(result.cost, 10**15 * (n - 1))
        self.assertIsInstance(result.cost, int)
        result.verify()


class MediumScaleTests(unittest.TestCase):
    def _grid_network(self, cols: int, rows: int) -> FlowNetwork:
        """带平行边与负费用边的网格图。"""
        n = cols * rows

        def node(c, r):
            return r * cols + c

        edges = []
        for r in range(rows):
            for c in range(cols):
                u = node(c, r)
                if c + 1 < cols:
                    v = node(c + 1, r)
                    edges.append((u, v, 1000, int((r + c) % 7 - 3)))
                    # 西向反向通道费用 4：任意东西向 2-环费用和 ≥ 1，
                    # 图中不存在北向边 ⇒ 不存在可达负费用环（满足输入前提）。
                    edges.append((v, u, 300, 4))
                if r + 1 < rows:
                    v = node(c, r + 1)
                    edges.append((u, v, 1000, int((c - r) % 5 - 2)))
        return FlowNetwork(n, 0, n - 1, edges)

    def test_grid_40x50_runs_fast(self) -> None:
        # n = 2000, m ≈ 11800 超过 MAX_EDGES；这里用小一档但仍属中规模：
        net = self._grid_network(cols=40, rows=40)  # n=1600, m≈9440
        self.assertLessEqual(net.n, limits.MAX_NODES)
        self.assertLessEqual(net.m, limits.MAX_EDGES)

        start = time.perf_counter()
        result = min_cost_max_flow(net)
        elapsed = time.perf_counter() - start

        result.verify()
        check_reduced_costs_nonnegative(net, result.potentials)
        self.assertGreater(result.flow, 0)
        # SSP 增广次数受瓶颈容量影响；网格容量 1000，通常几十次增广内完成。
        self.assertLess(
            elapsed, 20.0, f"中规模算例耗时 {elapsed:.2f}s 超过 20s 预算"
        )


if __name__ == "__main__":
    unittest.main()
