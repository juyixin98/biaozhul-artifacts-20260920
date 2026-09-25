"""精确解与独立网格暴力法的穷举对照测试（核心验收点）。

两种实现策略刻意不同（见 :mod:`packing.grid_bruteforce` 模块 docstring）：
一个是候选位置 + 稳定性剪枝 DFS，一个是逐整数格点暴力。在所有
n <= 6、W <= 8、尺寸 1..W 的随机小算例上，二者给出的最优高度必须一致。
"""

import unittest

import numpy as np

from packing.bounds import all_lower_bounds, validate_request
from packing.exact import solve_exact
from packing.grid_bruteforce import grid_optimal_height
from packing.heuristics import best_heuristic
from packing.layout import verify_layout


def inst(sw, rects):
    return validate_request({"strip_width": sw,
                             "rectangles": [{"width": w, "height": h} for w, h in rects]})


class TestExactVsGridBruteforce(unittest.TestCase):

    def _assert_agree(self, sw, rects):
        i = inst(sw, rects)
        ex = solve_exact(i)
        self.assertEqual(ex.status, "optimal", (sw, rects, ex.reason))
        gh = grid_optimal_height(i)
        self.assertIsNotNone(gh, "网格暴力法未找到可行高度: %s" % (rects,))
        self.assertEqual(
            int(round(ex.height)), gh,
            "\n候选位置DFS 最优=%s 但网格暴力最优=%s\n算例: W=%s rects=%s"
            % (ex.height, gh, sw, rects),
        )
        # 精确布局自身必须合法
        rep = verify_layout(i, ex.x, ex.y)
        self.assertTrue(rep.ok, rep.as_dict())
        self.assertAlmostEqual(rep.height, ex.height, places=9)

        # 夹逼关系：有效下界 <= 真实最优 <= 启发式高度
        lbs = all_lower_bounds(i)
        heur = best_heuristic(i)
        for name, lb in lbs.items():
            self.assertLessEqual(lb, ex.height + 1e-9,
                                 "下界 %s=%g 超过真实最优 %g" % (name, lb, ex.height))
        self.assertLessEqual(ex.height, heur.height + 1e-9,
                             "真实最优 %g 高于启发式 %g（启发式坏了）"
                             % (ex.height, heur.height))

    def test_handcrafted_cases(self):
        cases = [
            (5, [(2, 3), (2, 3), (2, 3)]),
            (10, [(6, 2), (7, 3), (8, 4)]),
            (4, [(2, 2), (2, 2), (2, 2), (2, 2)]),
            (6, [(3, 5), (4, 2), (2, 4), (5, 1)]),
            (3, [(1, 1), (1, 1), (1, 1), (1, 1), (1, 1), (1, 1)]),
        ]
        for sw, rects in cases:
            with self.subTest(sw=sw, rects=rects):
                self._assert_agree(sw, rects)

    def test_exhaustive_two_rectangles(self):
        # 两件矩形：穷举 W=1..5、w=1..W、h=1..4 的全部组合
        for W in range(1, 6):
            for w1 in range(1, W + 1):
                for w2 in range(1, W + 1):
                    for h1 in range(1, 4):
                        for h2 in range(1, 4):
                            self._assert_agree(float(W),
                                               [(float(w1), float(h1)),
                                                (float(w2), float(h2))])

    def test_random_small_instances_agree(self):
        rng = np.random.default_rng(424242)
        checked = 0
        for _ in range(60):
            n = int(rng.integers(1, 7))           # n <= 6（网格法上限）
            sw = int(rng.integers(3, 9))          # W <= 8
            rects = [(float(rng.integers(1, sw + 1)),
                      float(rng.integers(1, 6)))
                     for _ in range(n)]
            self._assert_agree(float(sw), rects)
            checked += 1
        self.assertGreaterEqual(checked, 60)

    def test_exact_optimum_matches_known_packing(self):
        # 经典小例：两个 3×3 放进 W=6 ⇒ 恰好高 3
        i = inst(6, [(3, 3), (3, 3)])
        ex = solve_exact(i)
        self.assertEqual(ex.status, "optimal")
        self.assertEqual(ex.height, 3.0)

    def test_exact_skipped_for_large_instance(self):
        i = inst(10, [(2, 2)] * 9)  # n=9 > 8
        ex = solve_exact(i)
        self.assertEqual(ex.status, "skipped")
        self.assertIn("超过精确求解上限", ex.reason)

    def test_exact_skipped_for_fractional_instance(self):
        i = inst(10, [(2.5, 3), (2.5, 3)])
        ex = solve_exact(i)
        self.assertEqual(ex.status, "skipped")
        self.assertIn("整数", ex.reason)


if __name__ == "__main__":
    unittest.main()
