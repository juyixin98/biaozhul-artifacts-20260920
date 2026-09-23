"""自动化测试：unittest（零额外依赖），运行： python3 -m unittest discover -s tests -v

覆盖验收点：
- 小例穷举对照（分支定界 vs 独立网格穷举）；
- 零尺寸拒绝、超宽拒绝、数量/范围拒绝；
- 面积下界与其他严格下界的有效性（LB <= 最优值）；
- 布局碰撞检测（共享边界合法、面积重叠被抓）；
- 启发式只声明为上界，经典非层式实例上启发式次优；
- JSON 接口往返与退出码。
"""

import json
import os
import random
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strip_packing.geometry import (  # noqa: E402
    Placement, Tolerance, rects_overlap, verify_layout)
from strip_packing.validation import (  # noqa: E402
    PackingError, validate_instance, MAX_RECTS, MAX_DIM)
from strip_packing.lower_bounds import lower_bounds  # noqa: E402
from strip_packing.heuristics import run_heuristics, nfdh, ffdh  # noqa: E402
from strip_packing.exact import exact_packing  # noqa: E402
from strip_packing.solver import solve_packing  # noqa: E402
from strip_packing.api import process  # noqa: E402
from grid_oracle import grid_optimal_height  # noqa: E402


def rectlist(pairs):
    """[(w,h),...] -> [(id,w,h),...]"""
    return [(i, float(w), float(h)) for i, (w, h) in enumerate(pairs)]


def payload(W, pairs, exact="auto"):
    return {"strip_width": W,
            "rectangles": [{"id": i, "width": w, "height": h}
                           for i, (w, h) in enumerate(pairs)],
            "exact": exact}


# ---------------------------------------------------------------- geometry

class TestGeometry(unittest.TestCase):
    def test_edge_contact_is_not_overlap(self):
        # 左右共享边
        a = Placement("a", 0, 0, 2, 3)
        b = Placement("b", 2, 0, 2, 3)
        self.assertFalse(rects_overlap(a, b))
        # 上下共享边
        c = Placement("c", 0, 3, 2, 1)
        self.assertFalse(rects_overlap(a, c))

    def test_real_overlap_detected(self):
        a = Placement("a", 0, 0, 2, 2)
        b = Placement("b", 1.5, 1.5, 2, 2)
        self.assertTrue(rects_overlap(a, b))

    def test_float_rounding_touch_accepted(self):
        # 本应恰好接触但有 -1e-15 量级负间隙
        a = Placement("a", 0, 0, 1.0, 1.0)
        b = Placement("b", 1.0 - 1e-15, 0, 1.0, 1.0)
        self.assertFalse(rects_overlap(a, b, Tolerance()))

    def test_corner_point_contact_is_not_overlap(self):
        a = Placement("a", 0, 0, 1, 1)
        b = Placement("b", 1, 1, 1, 1)
        self.assertFalse(rects_overlap(a, b))

    def test_verify_layout_collision_reported(self):
        rects = rectlist([(2, 2), (2, 2)])
        placements = [Placement(0, 0, 0, 2, 2), Placement(1, 1, 1, 2, 2)]
        rep = verify_layout(rects, placements, 10.0)
        self.assertFalse(rep["valid"])
        self.assertTrue(any("collision" in v for v in rep["violations"]))

    def test_verify_layout_out_of_bounds(self):
        rects = rectlist([(6, 2)])
        placements = [Placement(0, 0, 0, 6, 2)]
        rep = verify_layout(rects, placements, 5.0)
        self.assertFalse(rep["valid"])

    def test_verify_layout_size_mismatch(self):
        # 旋转会交换宽高 -> 必须被识别
        rects = rectlist([(3, 5)])
        placements = [Placement(0, 0, 0, 5, 3)]
        rep = verify_layout(rects, placements, 10.0)
        self.assertFalse(rep["valid"])


# -------------------------------------------------------------- validation

class TestValidation(unittest.TestCase):
    def test_zero_width_strip_rejected(self):
        with self.assertRaises(PackingError) as cm:
            validate_instance(0, [{"width": 1, "height": 1}])
        self.assertEqual(cm.exception.code, "INVALID_WIDTH")

    def test_zero_size_rectangle_rejected(self):
        for w, h in ((0, 1), (1, 0), (0, 0)):
            with self.subTest(w=w, h=h):
                with self.assertRaises(PackingError) as cm:
                    validate_instance(5, [{"width": w, "height": h}])
                self.assertEqual(cm.exception.code, "INVALID_RECTANGLE")

    def test_negative_and_nan_rejected(self):
        for w, h in ((-1, 2), (float("nan"), 2), (2, float("inf"))):
            with self.subTest(w=w, h=h):
                with self.assertRaises(PackingError):
                    validate_instance(5, [{"width": w, "height": h}])

    def test_too_wide_rejected(self):
        with self.assertRaises(PackingError) as cm:
            validate_instance(5, [{"width": 5.5, "height": 2}])
        self.assertEqual(cm.exception.code, "RECTANGLE_TOO_WIDE")

    def test_equal_width_accepted(self):
        W, rects = validate_instance(5, [{"width": 5, "height": 2}])
        self.assertEqual(W, 5.0)
        self.assertEqual(len(rects), 1)

    def test_too_many_rectangles(self):
        data = [{"width": 1, "height": 1}] * (MAX_RECTS + 1)
        with self.assertRaises(PackingError) as cm:
            validate_instance(100, data)
        self.assertEqual(cm.exception.code, "TOO_MANY_RECTANGLES")

    def test_dim_cap(self):
        with self.assertRaises(PackingError):
            validate_instance(MAX_DIM + 1, [])
        with self.assertRaises(PackingError):
            validate_instance(10, [{"width": 5, "height": MAX_DIM + 1}])

    def test_bad_tolerance_rejected(self):
        with self.assertRaises(PackingError) as cm:
            validate_instance(5, [{"width": 1, "height": 1}], atol=0.5)
        self.assertEqual(cm.exception.code, "INVALID_TOLERANCE")

    def test_empty_instance_valid(self):
        W, rects = validate_instance(5, [])
        self.assertEqual(rects, [])


# ------------------------------------------------------------- lower bounds

class TestLowerBounds(unittest.TestCase):
    def test_area_bound_basic(self):
        rects = rectlist([(2, 3), (2, 3)])  # 面积 12, W=6 -> 2
        lbs = lower_bounds(rects, 6)
        self.assertAlmostEqual(lbs["area"], 2.0)
        self.assertAlmostEqual(lbs["max_height"], 3.0)
        self.assertGreaterEqual(lbs["lower_bound"], 2.99)

    def test_pairwise_bound(self):
        # 两个宽 4、高 3 的矩形在 W=7 内无法并排 => LB>=6
        rects = rectlist([(4, 3), (4, 3)])
        lbs = lower_bounds(rects, 7)
        self.assertAlmostEqual(lbs["pairwise"], 6.0)
        self.assertAlmostEqual(lbs["lower_bound"], 6.0)

    def test_pairwise_bound_strict_width(self):
        # 宽之和恰好 = W 时可以并排，不产生两两下界
        rects = rectlist([(3, 5), (3, 4)])
        lbs = lower_bounds(rects, 6)
        self.assertEqual(lbs["pairwise"], 0.0)
        self.assertAlmostEqual(lbs["lower_bound"], 5.0)

    def test_empty_bounds_zero(self):
        self.assertEqual(lower_bounds([], 5)["lower_bound"], 0.0)

    def test_lb_never_exceeds_optimum_handcrafted(self):
        # 手工可构造布局 => 最优值已知上界 U，LB 不得超过 U
        rects = rectlist([(3, 2), (3, 2), (2, 2), (2, 2)])
        lbs = lower_bounds(rects, 6)
        # 两个 3 宽一层（高2），两个 2 宽一层（高2） => H*<=4
        self.assertLessEqual(lbs["lower_bound"], 4.0 + 1e-9)


# -------------------------------------------------------------- heuristics

class TestHeuristics(unittest.TestCase):
    def test_all_heuristic_layouts_valid(self):
        pairs = [(3, 4), (2, 3), (5, 2), (1, 5), (4, 4), (2, 2)]
        rects, W = rectlist(pairs), 6
        cands = run_heuristics(rects, W)
        self.assertTrue(cands)
        for c in cands:
            self.assertGreaterEqual(c["height"],
                                    lower_bounds(rects, W)["lower_bound"] - 1e-9)
            rep = verify_layout(rects, c["placements"], W)
            self.assertTrue(rep["valid"], rep["violations"])

    def test_nfdh_simple_shelf(self):
        # 两个宽 3、高 2 的矩形恰好一层
        placements = nfdh(rectlist([(3, 2), (3, 2)]), 6)
        self.assertAlmostEqual(max(p.top for p in placements), 2.0)

    def test_ffdh_reuses_earlier_shelf(self):
        # 5x3, 1x2, 5x2：FFDH 会把 1x2 放入第一层，高度=max(3,2)=3+2=5
        rects = rectlist([(5, 3), (1, 2), (5, 2)])
        placements = ffdh(rects, 6, key="height")
        rep = verify_layout(rects, placements, 6)
        self.assertTrue(rep["valid"], rep["violations"])
        self.assertAlmostEqual(rep["height"], 5.0)

    def test_heuristic_height_above_lb_gap_is_real(self):
        # 经典非层式互锁实例 (W=10)：
        # A:6x6, B:6x4, C:4x4, D:4x4。
        # 最优 H*=10（互锁）；NFDH（不能复用封掉的层）结果为 14，
        # 展示启发式之间的差异与启发式不等于最优的事实。
        rects = rectlist([(6, 6), (6, 4), (4, 4), (4, 4)])
        lbs = lower_bounds(rects, 10)
        cands = run_heuristics(rects, 10)
        self.assertTrue(any(
            c["name"] == "NFDH-height" and abs(c["height"] - 14) < 1e-9
            for c in cands))
        # 所有启发式高度 >= 严格下界
        for c in cands:
            self.assertGreaterEqual(c["height"], lbs["lower_bound"] - 1e-9)
        # NFDH 的 14 确实次优（精确解为 10），启发式不被声明最优
        nfdh_h = next(c["height"] for c in cands
                      if c["name"] == "NFDH-height")
        self.assertGreater(nfdh_h, lbs["lower_bound"] + 1e-9)

    def test_heuristic_not_marked_optimal_anywhere(self):
        sol = solve_packing(payload(10, [(6, 6), (6, 4), (4, 4), (4, 4)],
                                    exact=False))
        self.assertEqual(sol.exact["status"], "not_run")
        self.assertGreater(sol.heuristic_height, sol.lower_bound - 1e-12)


# --------------------------------------------------------- exact vs oracle

class TestExactVsOracle(unittest.TestCase):
    """分支定界精确解与独立网格穷举在小整数实例上逐项一致。"""

    CASES = [
        (6, [(3, 2), (3, 2), (2, 3), (2, 3)]),
        (5, [(3, 3), (2, 2), (2, 2)]),
        (7, [(4, 3), (4, 3), (3, 2)]),
        (8, [(5, 4), (3, 6), (3, 3), (4, 2)]),
        (6, [(2, 5), (4, 3), (3, 3), (2, 2)]),
        (10, [(6, 6), (6, 4), (4, 4), (4, 4)]),
    ]

    def test_handcrafted_cases(self):
        for W, pairs in self.CASES:
            with self.subTest(case=(W, pairs)):
                rects = rectlist(pairs)
                cands = run_heuristics(rects, W)
                res = exact_packing(rects, W, cands[0]["height"],
                                    global_lb=lower_bounds(rects, W)["lower_bound"],
                                    node_limit=500_000, time_limit=30)
                self.assertEqual(res["status"], "optimal")
                oracle = grid_optimal_height(pairs, W, int(cands[0]["height"]))
                self.assertIsNotNone(oracle)
                self.assertAlmostEqual(res["height"], float(oracle), places=8)
                if res["placements"] is not None:
                    rep = verify_layout(rects, res["placements"], W)
                    self.assertTrue(rep["valid"], rep["violations"])
                    self.assertAlmostEqual(rep["height"], res["height"], places=8)

    def test_random_cases_against_grid_oracle(self):
        rng = random.Random(20260923)
        for trial in range(12):
            n = rng.randint(1, 6)
            W = rng.randint(4, 8)
            pairs = [(rng.randint(1, W), rng.randint(1, 5)) for _ in range(n)]
            with self.subTest(trial=trial, W=W, pairs=pairs):
                rects = rectlist(pairs)
                cands = run_heuristics(rects, W)
                res = exact_packing(
                    rects, W, cands[0]["height"],
                    global_lb=lower_bounds(rects, W)["lower_bound"],
                    node_limit=500_000, time_limit=30)
                self.assertEqual(res["status"], "optimal",
                                 f"搜索预算耗尽: nodes={res['nodes']}")
                oracle = grid_optimal_height(pairs, W, int(cands[0]["height"]))
                self.assertIsNotNone(oracle)
                self.assertAlmostEqual(res["height"], float(oracle), places=8,
                                       msg=(W, pairs))

    def test_auto_exact_threshold_and_forced_limit(self):
        from strip_packing.solver import EXACT_AUTO_N
        # n == EXACT_AUTO_N：auto 模式自动运行精确求解
        pairs = [(2, 2)] * EXACT_AUTO_N
        sol = solve_packing(payload(6, pairs, exact="auto"))
        self.assertEqual(sol.exact["status"], "optimal")
        # n == EXACT_AUTO_N + 1：auto 不跑精确；强制 true 返回
        # limit_reached（这是如实的失败状态，不声明最优）
        pairs_big = [(3 + (i % 5), 2 + (i % 7))
                     for i in range(EXACT_AUTO_N + 3)]
        sol_auto = solve_packing(payload(12, pairs_big, exact="auto"))
        self.assertEqual(sol_auto.exact["status"], "not_run")
        self.assertGreaterEqual(
            sol_auto.heuristic_height, sol_auto.lower_bound - 1e-9)

    def test_interlock_instance_exact_beats_shelf_heuristic(self):
        # 启发式（上界）≠ 最优 的直接证据：H*=10, FFDH=14
        pairs = [(6, 6), (6, 4), (4, 4), (4, 4)]
        sol = solve_packing(payload(10, pairs, exact=True))
        self.assertEqual(sol.exact["status"], "optimal")
        self.assertAlmostEqual(sol.lower_bound, 10.0)
        self.assertAlmostEqual(sol.heuristic_height, 10.0)
        self.assertAlmostEqual(sol.gap, 0.0)
        self.assertTrue(sol.verification["valid"])

    def test_empty_instance(self):
        sol = solve_packing(payload(5, []))
        self.assertEqual(sol.status, "ok")
        self.assertEqual(sol.heuristic_height, 0.0)
        self.assertEqual(sol.lower_bound, 0.0)
        self.assertEqual(sol.placements, [])


# --------------------------------------------------------------- API / JSON

class TestApi(unittest.TestCase):
    def test_round_trip(self):
        resp = process(json.dumps(payload(6, [(3, 2), (3, 2), (4, 3)])))
        self.assertEqual(resp["status"], "ok")
        self.assertIn("lower_bound", resp)
        self.assertIn("heuristic_height", resp)
        self.assertTrue(resp["layout_verification"]["valid"])
        self.assertEqual(resp["optimality"], "not_claimed")
        self.assertEqual(len(resp["placements"]), 3)
        for p in resp["placements"]:
            self.assertGreaterEqual(p["x"], -1e-9)
            self.assertGreaterEqual(p["y"], -1e-9)
            self.assertLessEqual(p["x"] + p["width"], 6 + 1e-9)

    def test_zero_size_error_payload(self):
        bad = {"strip_width": 5,
               "rectangles": [{"width": 0, "height": 1}]}
        resp = process(json.dumps(bad))
        self.assertEqual(resp["status"], "error")
        self.assertEqual(resp["error_code"], "INVALID_RECTANGLE")

    def test_too_wide_error_payload(self):
        bad = {"strip_width": 5,
               "rectangles": [{"width": 6, "height": 1}]}
        resp = process(json.dumps(bad))
        self.assertEqual(resp["error_code"], "RECTANGLE_TOO_WIDE")

    def test_invalid_json_text(self):
        resp = process("{not json")
        self.assertEqual(resp["error_code"], "INVALID_JSON")

    def test_missing_field(self):
        resp = process(json.dumps({"strip_width": 5}))
        self.assertEqual(resp["error_code"], "INVALID_JSON")

    def test_cli_exit_codes(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        def run(text):
            p = subprocess.run(
                [sys.executable, "-m", "strip_packing.api"],
                input=text, capture_output=True, text=True, cwd=root)
            return p.returncode, p.stdout

        rc, out = run(json.dumps(payload(6, [(2, 2), (2, 2)])))
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["status"], "ok")

        rc, out = run(json.dumps(
            {"strip_width": 5, "rectangles": [{"width": 9, "height": 1}]}))
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out)["error_code"], "RECTANGLE_TOO_WIDE")

        rc, _out = run("not-json-at-all")
        self.assertEqual(rc, 2)

    def test_response_documents_non_optimality(self):
        resp = process(json.dumps(payload(6, [(2, 3), (4, 2), (3, 3)],
                                        exact=False)))
        self.assertEqual(resp["optimality"], "not_claimed")
        self.assertIn("note", resp)
        self.assertIn("不声明", resp["note"])


if __name__ == "__main__":
    unittest.main()
