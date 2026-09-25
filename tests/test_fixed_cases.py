"""固定用例：平行边、负费用边与反向增广（撤销先前流量）。"""

from __future__ import annotations

import unittest

from mcf.graph import FlowNetwork
from mcf.solver import min_cost_max_flow

from .brute_force import enumerate_min_cost


class ParallelEdgeTests(unittest.TestCase):
    def test_parallel_edges_choose_cheapest_first(self) -> None:
        # s=0 -> t=1：两条平行边，费用 1（容量 3）与费用 4（容量 2）。
        edges = [(0, 1, 3, 1), (0, 1, 2, 4)]
        result = min_cost_max_flow(FlowNetwork(2, 0, 1, edges))
        self.assertEqual(result.flow, 5)
        self.assertEqual(result.cost, 3 * 1 + 2 * 4)
        self.assertEqual(result.edge_flows, [3, 2])
        result.verify()

        # 逐流量对照穷举
        truth = enumerate_min_cost(2, 0, 1, edges)
        self.assertEqual(truth[1], 1)
        self.assertEqual(truth[3], 3)
        self.assertEqual(truth[4], 7)
        self.assertEqual(truth[5], 11)

    def test_parallel_negative_edges(self) -> None:
        edges = [(0, 1, 2, -5), (0, 1, 2, 5), (0, 1, 1, -5)]
        result = min_cost_max_flow(FlowNetwork(2, 0, 1, edges))
        self.assertEqual(result.flow, 5)
        self.assertEqual(result.cost, 3 * (-5) + 2 * 5)
        result.verify()


class NegativeEdgeTests(unittest.TestCase):
    def test_negative_edges_without_negative_cycle(self) -> None:
        # 负费用边 1->0 构成 2 环，但正边 0->1 费用 +3，环费用 +1（非负环）。
        edges = [(0, 1, 5, 3), (1, 0, 5, -2), (0, 2, 4, 10), (2, 1, 4, 4)]
        result = min_cost_max_flow(FlowNetwork(3, 0, 1, edges))
        # 直连 5 单位（费用 3），其余经 0->2->1（费用 14）。
        self.assertEqual(result.flow, 9)
        self.assertEqual(result.cost, 5 * 3 + 4 * 14)
        result.verify()

        truth = enumerate_min_cost(3, 0, 1, edges)
        for f in range(10):
            net = FlowNetwork(3, 0, 1, edges)
            r = min_cost_max_flow(net, required_flow=f)
            self.assertEqual((r.flow, r.cost), (f, truth[f]))


class ReverseAugmentationTests(unittest.TestCase):
    """构造必须沿反向弧增广（撤销先前决策）才能得到最优解的图。"""

    def test_greedy_then_reroute(self) -> None:
        # 经典例子：
        #   0->1 cap2 cost 0 ; 0->2 cap1 cost 0
        #   1->2 cap2 cost 1 （唯一通道）
        #   1->3 cap2 cost -100（极便宜）
        #   2->3 cap2 cost 0
        # 增广 2 单位后，再增广第 3 单位时必须：
        #   0->2 -> 沿 1->2 的反向弧 2->1 撤销 1 单位 -> 1->3，
        #   被撤销的 1->2 流量改由 2->3 直达。
        edges = [
            (0, 1, 2, 0),
            (0, 2, 1, 0),
            (1, 2, 2, 1),
            (1, 3, 2, -100),
            (2, 3, 2, 0),
        ]
        result = min_cost_max_flow(FlowNetwork(4, 0, 3, edges))
        self.assertEqual(result.flow, 3)
        # 最优：2 单位走 0->1->3，1 单位走 0->2->3；0->1->2 上流量为 0。
        self.assertEqual(result.cost, -200)
        self.assertEqual(result.edge_flows, [2, 1, 0, 2, 1])
        result.verify()

        # 穷举逐流量对照
        truth = enumerate_min_cost(4, 0, 3, edges, max_augment_steps=6)
        expected = {0: 0, 1: -100, 2: -200, 3: -200}
        self.assertEqual({k: truth[k] for k in expected}, expected)
        for f in range(4):
            net = FlowNetwork(4, 0, 3, edges)
            r = min_cost_max_flow(net, required_flow=f)
            self.assertEqual((r.flow, r.cost), (f, truth[f]))

    def test_explicit_reverse_arc_in_intermediate_path(self) -> None:
        # 更直接的反向增广场景：
        #   0->1 cap1 cost 0, 0->2 cap1 cost 0,
        #   1->2 cap1 cost 1, 1->3 cap1 cost -10, 2->3 cap1 cost 0。
        # 最大流 2，最优：0->1->3 (-10) 与 0->2->3 (0)，费用 -10。
        # 若第一单位贪心取 0->1->2->3，第二单位必须反向撤销 1->2。
        edges = [
            (0, 1, 1, 0),
            (0, 2, 1, 0),
            (1, 2, 1, 1),
            (1, 3, 1, -10),
            (2, 3, 1, 0),
        ]
        result = min_cost_max_flow(FlowNetwork(4, 0, 3, edges))
        self.assertEqual((result.flow, result.cost), (2, -10))
        result.verify()
        truth = enumerate_min_cost(4, 0, 3, edges)
        self.assertEqual(truth[2], -10)


if __name__ == "__main__":
    unittest.main()
