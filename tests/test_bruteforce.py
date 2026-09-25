"""穷举对照：随机小图上比较 SSP 算法与可行流穷举器。

覆盖：
- 任意随机小图（含平行边、自环、零容量边、负费用边）；
- 每个可行流量（穷举器给出）下的最小费用一致；
- 最大流值一致；
- 不可达汇点时最大流为 0；
- 生产代码输出通过容量约束与流守恒自检（solver 内部已 assert，
  这里再显式断言一遍）。
"""

from __future__ import annotations

import unittest

import numpy as np

from mcf.graph import FlowNetwork
from mcf.solver import min_cost_max_flow

from .brute_force import (
    enumerate_min_cost,
    has_reachable_negative_cycle,
    random_edges,
)


class BruteForceComparisonTests(unittest.TestCase):
    def test_random_graphs_against_enumeration(self) -> None:
        rng = np.random.default_rng(20260924)
        graph_count = 0
        flow_points = 0

        # 200 个候选小图；跳过含源点可达负环的图（输入前提不允许）。
        for _ in range(200):
            n = int(rng.integers(2, 7))           # 2..6 个顶点
            s = int(rng.integers(0, n))
            t = int(rng.integers(0, n))
            if s == t:
                continue
            m = int(rng.integers(0, 9))           # 0..8 条边
            edges = random_edges(
                rng, n, m, max_cap=2, cost_range=3
            )

            if has_reachable_negative_cycle(n, s, edges):
                continue
            graph_count += 1

            truth = enumerate_min_cost(n, s, t, edges)
            net = FlowNetwork(n, s, t, edges)
            result = min_cost_max_flow(net)

            # 最大流值一致
            self.assertEqual(
                result.flow,
                max(truth),
                msg=f"最大流不一致: n={n}, s={s}, t={t}, edges={edges}",
            )
            # 各可行流量下最小费用一致
            for f, c in truth.items():
                net2 = FlowNetwork(n, s, t, edges)
                r2 = min_cost_max_flow(net2, required_flow=f)
                self.assertEqual(r2.status, "optimal")
                self.assertEqual(r2.flow, f)
                self.assertEqual(
                    r2.cost,
                    c,
                    msg=(
                        f"流量 {f} 的最小费用不一致: "
                        f"n={n}, s={s}, t={t}, edges={edges}"
                    ),
                )
                r2.verify()
                flow_points += 1
            result.verify()

        self.assertGreaterEqual(graph_count, 100, "有效图数量过少，测试不充分")
        self.assertGreater(flow_points, 0)

    def test_unreachable_sink_zero_flow(self) -> None:
        # 汇点 3 完全孤立：最大流必须为 0，且不报错。
        edges = [(0, 1, 5, 2), (1, 0, 5, -2), (1, 2, 3, 1)]
        truth = enumerate_min_cost(4, 0, 3, edges)
        net = FlowNetwork(4, 0, 3, edges)
        result = min_cost_max_flow(net)
        self.assertEqual(result.flow, 0)
        self.assertEqual(result.cost, 0)
        self.assertEqual(result.status, "optimal")
        self.assertTrue(result.max_flow_reached)
        self.assertEqual(set(truth), {0})
        result.verify()

    def test_required_flow_infeasible_on_isolated_sink(self) -> None:
        edges = [(0, 1, 5, 2)]
        net = FlowNetwork(3, 0, 2, edges)
        result = min_cost_max_flow(net, required_flow=1)
        self.assertEqual(result.status, "infeasible")
        self.assertEqual(result.flow, 0)
        result.verify()


if __name__ == "__main__":
    unittest.main()
