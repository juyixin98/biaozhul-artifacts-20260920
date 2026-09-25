"""核心算法测试：Pareto 前沿、支配、等权标签、零权环、路径重建。"""

from __future__ import annotations

import unittest

import numpy as np

from mospp.enumeration import enumerate_simple_paths
from mospp.labeling import solve, verify_path
from mospp.tolerance import (
    dominates,
    objectives_equal,
    pareto_mask,
)

from .helpers import front_points, make_graph, random_graph


class TestTolerance(unittest.TestCase):
    def test_scalar_dominance(self):
        self.assertTrue(dominates((1.0, 2.0), (1.0, 3.0), 0.0, 0.0))
        self.assertTrue(dominates((1.0, 2.0), (2.0, 2.0), 0.0, 0.0))
        self.assertTrue(dominates((1.0, 2.0), (3.0, 4.0), 0.0, 0.0))
        # 两维都相等 -> 不支配（弱序需要一维严格）
        self.assertFalse(dominates((1.0, 2.0), (1.0, 2.0), 0.0, 0.0))
        # 互不支配
        self.assertFalse(dominates((1.0, 4.0), (2.0, 3.0), 0.0, 0.0))
        self.assertFalse(dominates((2.0, 3.0), (1.0, 4.0), 0.0, 0.0))

    def test_tolerance_band_is_neutral(self):
        # 差值落在容差带内：既不支配也不算严格更差
        self.assertFalse(dominates((1.0, 2.0), (1.0 + 5e-10, 2.0), 1e-9, 0.0))
        self.assertFalse(dominates((1.0 + 5e-10, 2.0), (1.0, 2.0), 1e-9, 0.0))
        self.assertTrue(
            objectives_equal((1.0, 2.0), (1.0 + 5e-10, 2.0), 1e-9, 0.0)
        )
        # 超出容差带：正常支配
        self.assertTrue(dominates((1.0, 2.0), (1.0 + 1e-6, 2.0), 1e-9, 0.0))

    def test_pareto_mask_duplicates_kept(self):
        pts = np.array(
            [[0.0, 5.0], [0.0, 0.0], [1.0, 1.0], [2.0, 2.0]]
        )
        mask = pareto_mask(pts, 0.0, 0.0)
        # (0,5) 被 (0,0) 支配；(1,1) 被 (0,0) 支配；(2,2) 同；(0,0) 留下
        self.assertEqual(mask.tolist(), [False, True, False, False])

    def test_pareto_mask_equal_points_coexist(self):
        pts = np.array([[1.0, 2.0], [1.0, 2.0], [0.0, 5.0], [3.0, 1.0]])
        mask = pareto_mask(pts, 0.0, 0.0)
        # 两个等权点 (1,2) 互不支配，都保留；(0,5) 与 (1,2) 互不支配也保留；
        # (3,1) 与其他点互不支配（时间大但费用小），同样保留。
        self.assertTrue(mask[0])
        self.assertTrue(mask[1])
        self.assertTrue(mask[2])
        self.assertTrue(mask[3])

    def test_pareto_mask_strict_domination(self):
        pts = np.array([[2.0, 2.0], [1.0, 2.0], [1.0, 1.0]])
        mask = pareto_mask(pts, 0.0, 0.0)
        # (2,2) 被 (1,1) 支配；(1,2) 被 (1,1) 支配；只有 (1,1) 留下
        self.assertEqual(mask.tolist(), [False, False, True])


class TestHandBuiltGraphs(unittest.TestCase):
    def test_diamond_front(self):
        # A->B(1,4) A->C(2,2) B->D(3,1) C->D(4,1) B->C(1,1)
        g = make_graph(
            4,
            [
                (0, 1, 1, 4),
                (0, 2, 2, 2),
                (1, 3, 3, 1),
                (2, 3, 4, 1),
                (1, 2, 1, 1),
            ],
        )
        r = solve(g, 0, 3)
        self.assertEqual(
            front_points(r), [(4.0, 5.0), (6.0, 3.0)]
        )
        self.assertEqual(r.status, "ok")
        self.assertFalse(r.truncated)
        # A-B-C-D = (5,6) 被 (4,5) 支配
        paths = [tuple(p) for p in r.paths]
        self.assertIn((0, 1, 3), paths)
        self.assertIn((0, 2, 3), paths)
        self.assertNotIn((0, 1, 2, 3), paths)

    def test_disconnected_no_path(self):
        g = make_graph(3, [(0, 1, 1, 1)])
        r = solve(g, 0, 2)
        self.assertEqual(r.status, "no_path")
        self.assertEqual(r.labels, [])
        self.assertFalse(r.truncated)

    def test_trivial_path_source_equals_target(self):
        g = make_graph(2, [(0, 1, 3, 4)])
        r = solve(g, 0, 0)
        self.assertEqual(r.status, "ok")
        self.assertEqual(len(r.labels), 1)
        self.assertEqual((r.labels[0].time, r.labels[0].cost), (0.0, 0.0))
        self.assertEqual(r.paths[0], [0])
        self.assertEqual(r.edges_used[0], [])

    def test_parallel_edges_both_non_dominated(self):
        # 两条平行边互为权衡
        g = make_graph(2, [(0, 1, 1, 5), (0, 1, 4, 1)])
        r = solve(g, 0, 1)
        self.assertEqual(front_points(r), [(1.0, 5.0), (4.0, 1.0)])

    def test_parallel_edges_dominated(self):
        g = make_graph(2, [(0, 1, 1, 5), (0, 1, 4, 1), (0, 1, 4, 4)])
        r = solve(g, 0, 1)
        self.assertEqual(front_points(r), [(1.0, 5.0), (4.0, 1.0)])


class TestEqualLabels(unittest.TestCase):
    """相等标签：不同路径但目标向量相同。"""

    def test_two_equal_objective_paths_kept(self):
        # 0->1->3 = (2,4)，0->2->3 = (2,4)：两条路径等权。
        # simple 模式：两条都保留（主路径 + equal_paths 替代）；
        # walk 模式：目标相等视为重复标签只留一条（前沿点集仍精确）。
        g = make_graph(
            4,
            [
                (0, 1, 1, 2),
                (0, 2, 1, 3),
                (1, 3, 1, 2),
                (2, 3, 1, 1),
            ],
        )
        r_simple = solve(g, 0, 3, mode="simple")
        self.assertEqual(len(r_simple.entries), 1)
        entry = r_simple.entries[0]
        self.assertEqual((entry.time, entry.cost), (2.0, 4.0))
        all_paths_simple = {
            tuple(p)
            for p in [entry.nodes, *entry.alt_nodes]
        }
        self.assertEqual(all_paths_simple, {(0, 1, 3), (0, 2, 3)})

        r_walk = solve(g, 0, 3, mode="walk")
        self.assertEqual(len(r_walk.entries), 1)
        self.assertEqual(
            (r_walk.entries[0].time, r_walk.entries[0].cost), (2.0, 4.0)
        )

    def test_equal_objective_incomparable_visited_kept(self):
        # 在中间节点等权但 visited 不可比：两条路线仍应分别延伸到终点
        # 0->1 (1,1), 0->2 (1,1)；1->3 与 2->3 各 (1,1)；1/2 之间不通
        g = make_graph(
            4,
            [(0, 1, 1, 1), (0, 2, 1, 1), (1, 3, 1, 1), (2, 3, 1, 1)],
        )
        r = solve(g, 0, 3)
        self.assertEqual(len(r.entries), 1)
        entry = r.entries[0]
        paths = {tuple(p) for p in [entry.nodes, *entry.alt_nodes]}
        self.assertEqual(paths, {(0, 1, 3), (0, 2, 3)})


class TestZeroCycles(unittest.TestCase):
    def test_zero_weight_cycle_simple_mode(self):
        # 0<->1 零权双向环，1->2 正权；最优 (2,3)
        g = make_graph(
            3,
            [(0, 1, 0, 0), (1, 0, 0, 0), (1, 2, 2, 3)],
        )
        r = solve(g, 0, 2)
        self.assertEqual(r.status, "ok")
        self.assertEqual(front_points(r), [(2.0, 3.0)])
        # 零权环不应制造无限标签
        self.assertLessEqual(
            r.stats["labels_permanent"], 3 * 3  # 简单路径状态数有限上界
        )

    def test_zero_weight_cycle_walk_mode_terminates(self):
        g = make_graph(
            3,
            [(0, 1, 0, 0), (1, 0, 0, 0), (1, 2, 2, 3)],
        )
        r = solve(g, 0, 2, mode="walk")
        self.assertEqual(r.status, "ok")
        self.assertEqual(front_points(r), [(2.0, 3.0)])
        # 零权环产生的重复标签被丢弃，永久标签数有限
        self.assertLessEqual(r.stats["labels_permanent"], 3)

    def test_all_zero_weights(self):
        g = make_graph(
            4,
            [(0, 1, 0, 0), (0, 2, 0, 0), (1, 2, 0, 0), (2, 3, 0, 0)],
        )
        for mode in ("simple", "walk"):
            with self.subTest(mode=mode):
                r = solve(g, 0, 3, mode=mode)
                self.assertEqual(front_points(r), [(0.0, 0.0)])

    def test_positive_self_loop_ignored(self):
        g = make_graph(
            2,
            [(0, 1, 2, 2), (0, 0, 5, 5), (1, 1, 1, 1)],
        )
        for mode in ("simple", "walk"):
            with self.subTest(mode=mode):
                r = solve(g, 0, 1, mode=mode)
                self.assertEqual(front_points(r), [(2.0, 2.0)])


class TestPathReconstruction(unittest.TestCase):
    def test_reconstruct_and_verify(self):
        g = make_graph(
            5,
            [
                (0, 1, 1, 4),
                (0, 2, 2, 2),
                (1, 3, 3, 1),
                (2, 3, 4, 1),
                (1, 2, 1, 1),
                (3, 4, 1, 2),
            ],
        )
        for mode in ("simple", "walk"):
            r = solve(g, 0, 4, mode=mode)
            for lab, nodes, edges in zip(
                r.labels, r.paths, r.edges_used
            ):
                vt, vc = verify_path(g, nodes, edges, 0, 4)
                self.assertAlmostEqual(vt, lab.time, places=9)
                self.assertAlmostEqual(vc, lab.cost, places=9)
                # simple 模式路径不得有重复节点
                if mode == "simple":
                    self.assertEqual(len(nodes), len(set(nodes)))

    def test_reconstruct_direction_and_edges(self):
        g = make_graph(
            4,
            [(0, 1, 1, 4), (0, 2, 2, 2), (1, 3, 3, 1), (2, 3, 4, 1)],
        )
        r = solve(g, 0, 3)
        # 首节点 0、尾节点 3，边序列比节点序列短 1，且边首尾衔接
        for nodes, edges in zip(r.paths, r.edges_used):
            self.assertEqual(nodes[0], 0)
            self.assertEqual(nodes[-1], 3)
            self.assertEqual(len(edges), len(nodes) - 1)
            for k, e in enumerate(edges):
                self.assertEqual(int(g.edge_u[e]), nodes[k])
                self.assertEqual(int(g.edge_v[e]), nodes[k + 1])


class TestEnumerationCrossCheck(unittest.TestCase):
    """小图上标签算法 vs 全部简单路径枚举。"""

    def test_random_graphs_match_enumeration(self):
        import random

        rng = random.Random(20260924)
        checked = 0
        for _ in range(120):
            n = rng.randint(2, 9)
            g = random_graph(rng, n, rng.uniform(0.2, 0.55))
            s, t = 0, n - 1
            enum_res = enumerate_simple_paths(g, s, t)
            expected_paths = {tuple(p) for p in enum_res.paths}
            expected_pts = [(float(a), float(b)) for a, b in enum_res.points]
            for mode in ("simple", "walk"):
                r = solve(g, s, t, mode=mode)
                actual_pts = [(e.time, e.cost) for e in r.entries]
                self._assert_fronts_equal(
                    expected_pts,
                    actual_pts,
                    msg=f"mode={mode}, n={n}, m={g.m}",
                )
                # simple 模式：枚举给出的每条 Pareto 路径都必须能在
                # 主路径或 equal_paths 中找到
                if mode == "simple":
                    actual_paths = set()
                    for e in r.entries:
                        actual_paths.add(tuple(e.nodes))
                        actual_paths.update(
                            tuple(p) for p in e.alt_nodes
                        )
                    missing = expected_paths - actual_paths
                    self.assertFalse(
                        missing,
                        msg=(
                            f"n={n}, m={g.m}: simple 模式漏掉等权/非支配路径 "
                            f"{missing}\n枚举: {sorted(expected_paths)}\n"
                            f"标签: {sorted(actual_paths)}"
                        ),
                    )
                checked += 1
        self.assertGreater(checked, 100)

    def test_random_graphs_with_budgets(self):
        import random

        rng = random.Random(424242)
        for _ in range(60):
            n = rng.randint(3, 8)
            g = random_graph(rng, n, rng.uniform(0.25, 0.5))
            s, t = 0, n - 1
            all_pts = [
                (float(a), float(b))
                for a, b in enumerate_simple_paths(g, s, t).points
            ]
            if not all_pts:
                continue
            tb = float(rng.choice(sorted({p[0] for p in all_pts})))
            cb = float(rng.choice(sorted({p[1] for p in all_pts})))
            expected = enumerate_simple_paths(
                g, s, t, time_budget=tb, cost_budget=cb
            )
            for mode in ("simple", "walk"):
                r = solve(
                    g, s, t,
                    mode=mode, time_budget=tb, cost_budget=cb,
                )
                self._assert_fronts_equal(
                    [(float(a), float(b)) for a, b in expected.points],
                    [(l.time, l.cost) for l in r.labels],
                    msg=f"mode={mode},tb={tb},cb={cb}",
                )

    @staticmethod
    def _assert_fronts_equal(expected, actual, msg="", atol=1e-9, rtol=1e-9):
        """作为目标点 *集合* 相等。

        枚举可能对同一个 Pareto 点保留多条等权路径（点重复出现），
        标签接口则对相同目标向量做了分组，因此这里按集合比较。
        """
        def close(a, b):
            return objectives_equal(a, b, atol, rtol)

        missing = [
            e for e in expected
            if not any(close(e, a) for a in actual)
        ]
        extra = [
            a for a in actual
            if not any(close(a, e) for e in expected)
        ]
        if missing or extra:
            raise AssertionError(
                f"{msg}\n  expected={sorted(expected)}\n  actual={sorted(actual)}"
                f"\n  missing={missing}\n  extra={extra}"
            )


if __name__ == "__main__":
    unittest.main()
