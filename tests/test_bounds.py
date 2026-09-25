"""有效下界测试：面积界、最大件高界、宽件界，以及组合界的单调性。"""

import unittest

import numpy as np

from packing.bounds import (
    all_lower_bounds,
    area_lower_bound,
    max_height_lower_bound,
    validate_request,
    wide_items_lower_bound,
)


def inst(sw, rects):
    return validate_request({"strip_width": sw,
                             "rectangles": [{"width": w, "height": h} for w, h in rects]})


class TestLowerBounds(unittest.TestCase):

    # ---- 面积下界（验收点）----
    def test_area_bound_basic(self):
        # 总面积 3*4 + 6*2 = 24，W=10 ⇒ LB = 2.4
        i = inst(10, [(3, 4), (6, 2)])
        self.assertAlmostEqual(area_lower_bound(i), 2.4, places=9)

    def test_area_bound_full_width_strip(self):
        # 矩形恰好铺满 W×H：面积界应等于 H
        i = inst(4, [(2, 3), (2, 3), (4, 1)])  # 面积 6+6+4=16, /4 = 4
        self.assertAlmostEqual(area_lower_bound(i), 4.0, places=9)

    def test_area_bound_single(self):
        i = inst(10, [(5, 2)])
        self.assertAlmostEqual(area_lower_bound(i), 1.0, places=9)

    def test_max_height_bound(self):
        i = inst(10, [(3, 7), (2, 2), (4, 5)])
        self.assertEqual(max_height_lower_bound(i), 7.0)

    # ---- 宽件界 ----
    def test_wide_items_bound_sum_heights(self):
        # 三个宽度 > 5 的矩形不能两两同带 ⇒ H >= 2+3+4 = 9
        i = inst(10, [(6, 2), (7, 3), (8, 4), (2, 10)])
        self.assertEqual(wide_items_lower_bound(i), 9.0)

    def test_wide_items_exact_half_not_counted(self):
        # 宽恰为 W/2 的两件可以并排，不计入宽件界
        i = inst(10, [(5, 4), (5, 4)])
        self.assertEqual(wide_items_lower_bound(i), 0.0)

    def test_wide_items_just_over_half_counted(self):
        i = inst(10, [(5 + 1e-6, 4)])
        self.assertEqual(wide_items_lower_bound(i), 4.0)

    def test_combined_is_max(self):
        i = inst(10, [(6, 2), (7, 3), (8, 4), (2, 10)])
        lbs = all_lower_bounds(i)
        self.assertEqual(lbs["combined"],
                         max(lbs["area"], lbs["max_height"], lbs["wide_items"]))

    # ---- 有效性（validity）：下界永远不超过任何已知可行布局的高度 ----
    def test_bounds_never_exceed_heuristic_height(self):
        rng = np.random.default_rng(20260923)
        for trial in range(40):
            n = int(rng.integers(1, 12))
            sw = float(rng.integers(5, 15))
            rects = [(float(rng.integers(1, int(sw))), float(rng.integers(1, 8)))
                     for _ in range(n)]
            i = inst(sw, rects)
            lbs = all_lower_bounds(i)
            # 用一个朴素可行布局：所有矩形竖叠 ⇒ 高度 = sum(h)
            stacked_h = float(np.sum(i.heights))
            for name, lb in lbs.items():
                self.assertLessEqual(
                    lb, stacked_h + 1e-9,
                    "trial %d: 下界 %s=%g 超过可行布局高度 %g"
                    % (trial, name, lb, stacked_h))


if __name__ == "__main__":
    unittest.main()
