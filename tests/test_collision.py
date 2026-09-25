"""碰撞检测与布局合法性测试（验收点：布局碰撞检测）。"""

import unittest

import numpy as np

from packing.geometry import (
    intervals_overlap,
    placements_collide,
    rects_overlap,
)
from packing.bounds import validate_request
from packing.heuristics import best_heuristic
from packing.layout import verify_layout


def inst(sw, rects):
    return validate_request({"strip_width": sw,
                             "rectangles": [{"width": w, "height": h} for w, h in rects]})


class TestCollisionPrimitives(unittest.TestCase):

    def test_disjoint_intervals(self):
        self.assertFalse(intervals_overlap(0, 1, 1, 2))   # 端点贴合
        self.assertFalse(intervals_overlap(2, 3, 0, 1))
        self.assertTrue(intervals_overlap(0, 2, 1, 3))    # 正长度交集
        self.assertFalse(intervals_overlap(0, 1, 2, 3))

    def test_rects_touching_edges_do_not_collide(self):
        # 共边、共角都允许
        self.assertFalse(rects_overlap(0, 0, 2, 2, 2, 0, 2, 2))   # 左右相邻
        self.assertFalse(rects_overlap(0, 0, 2, 2, 0, 2, 2, 2))   # 上下相邻
        self.assertFalse(rects_overlap(0, 0, 1, 1, 1, 1, 1, 1))   # 角接触

    def test_rects_overlap_detected(self):
        self.assertTrue(rects_overlap(0, 0, 3, 3, 2, 2, 3, 3))
        self.assertTrue(rects_overlap(0, 0, 2, 2, 1, 0, 2, 2))    # 半宽重叠
        # 仅 x 重叠但 y 贴合 ⇒ 不碰撞
        self.assertFalse(rects_overlap(0, 0, 2, 1, 1, 1, 2, 1))

    def test_placements_collide_pair_list(self):
        x = np.array([0.0, 2.0, 0.0])
        y = np.array([0.0, 0.0, 2.0])
        w = np.array([2.0, 2.0, 2.0])
        h = np.array([2.0, 2.0, 2.0])
        # 0、1 共边；0、2 共边；1、2 仅角接触 ⇒ 全部无碰撞
        self.assertEqual(placements_collide(x, y, w, h), [])

        # 人为制造一个重叠
        x2 = np.array([0.0, 1.0])
        y2 = np.array([0.0, 0.0])
        w2 = np.array([2.0, 2.0])
        h2 = np.array([2.0, 2.0])
        self.assertEqual(placements_collide(x2, y2, w2, h2), [(0, 1)])


class TestHeuristicLayoutValidity(unittest.TestCase):
    """启发式对所有（含随机）算例都必须给出无重叠、不出界的布局。"""

    def test_handcrafted_layouts_valid(self):
        cases = [
            (10, [(6, 2), (7, 3), (8, 4), (2, 10)]),
            (10, [(10, 1), (10, 1), (10, 1)]),
            (5, [(2, 3), (2, 3), (2, 3)]),
            (10, [(1, 1)]),
        ]
        for sw, rects in cases:
            with self.subTest(sw=sw, rects=rects):
                i = inst(sw, rects)
                r = best_heuristic(i)
                rep = verify_layout(i, r.x, r.y)
                self.assertTrue(rep.ok, rep.as_dict())
                self.assertAlmostEqual(rep.height, r.height, places=9)

    def test_random_layouts_no_collision_in_bounds(self):
        rng = np.random.default_rng(777)
        for trial in range(120):
            n = int(rng.integers(1, 30))
            sw = float(rng.integers(3, 20))
            rects = [(float(rng.integers(1, int(sw) + 1)),
                      float(rng.integers(1, 10)))
                     for _ in range(n)]
            i = inst(sw, rects)
            r = best_heuristic(i)
            rep = verify_layout(i, r.x, r.y)
            self.assertTrue(
                rep.ok,
                "trial %d sw=%g rects=%s report=%s" % (trial, sw, rects, rep.as_dict()),
            )

    def test_force_collision_is_detected(self):
        """验收点：人为把两件叠放，verify_layout 必须报碰撞。"""
        i = inst(10, [(4, 4), (4, 4)])
        x = np.array([0.0, 0.0])
        y = np.array([0.0, 0.0])   # 完全重叠
        rep = verify_layout(i, x, y)
        self.assertFalse(rep.ok)
        self.assertEqual(rep.collisions, [(0, 1)])

    def test_force_out_of_bounds_detected(self):
        i = inst(10, [(6, 2)])
        rep = verify_layout(i, np.array([5.0]), np.array([0.0]))  # 5+6 > 10
        self.assertFalse(rep.ok)
        self.assertEqual(rep.out_of_bounds, [0])

    def test_negative_coordinates_detected(self):
        i = inst(10, [(2, 2)])
        rep = verify_layout(i, np.array([-1.0]), np.array([0.0]))
        self.assertFalse(rep.ok)
        self.assertEqual(rep.negative_coordinates, [0])


if __name__ == "__main__":
    unittest.main()
