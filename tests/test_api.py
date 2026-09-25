"""JSON 接口端到端测试：成功/失败响应结构、自检不变量、启发式非最优记录。"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

import numpy as np

from packing.api import solve
from packing.bounds import validate_request
from packing.exact import solve_exact
from packing.geometry import placements_collide
from packing.heuristics import best_heuristic

ROOT = Path(__file__).resolve().parent.parent


class TestAPI(unittest.TestCase):

    def test_success_response_shape(self):
        resp, code = solve({"strip_width": 10,
                            "rectangles": [{"width": 6, "height": 4},
                                           {"width": 5, "height": 3}]})
        self.assertEqual(code, 200)
        self.assertEqual(resp["status"], "ok")
        for key in ("input_summary", "lower_bounds", "heuristic",
                    "layout_verification", "gap"):
            self.assertIn(key, resp)
        for key in ("area", "max_height", "wide_items", "combined"):
            self.assertIn(key, resp["lower_bounds"])
        self.assertTrue(resp["layout_verification"]["ok"])
        self.assertEqual(resp["layout_verification"]["collisions"], [])
        self.assertEqual(len(resp["heuristic"]["placements"]), 2)
        # 绝不能把启发式标为最优保证
        self.assertIn("not proven optimal", resp["heuristic"]["guarantee"])

    def test_error_response_zero_size(self):
        resp, code = solve({"strip_width": 10,
                            "rectangles": [{"width": 0, "height": 3}]})
        self.assertEqual(code, 400)
        self.assertEqual(resp["status"], "error")
        self.assertEqual(resp["error"]["code"], "bad_rectangle_size")

    def test_error_response_too_wide(self):
        resp, code = solve({"strip_width": 10,
                            "rectangles": [{"width": 11, "height": 3}]})
        self.assertEqual(code, 400)
        self.assertEqual(resp["error"]["code"], "rectangle_too_wide")

    def test_exact_field_optimal_small(self):
        resp, code = solve({"strip_width": 6,
                            "rectangles": [{"width": 3, "height": 3},
                                           {"width": 3, "height": 3}],
                            "compute_exact": True})
        self.assertEqual(code, 200)
        self.assertEqual(resp["exact"]["status"], "optimal")
        self.assertEqual(resp["exact"]["height"], 3.0)

    def test_exact_field_skipped_large(self):
        resp, _ = solve({"strip_width": 10,
                         "rectangles": [{"width": 2, "height": 2}] * 10,
                         "compute_exact": True})
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(resp["exact"]["status"], "skipped")

    def test_no_exact_field_by_default(self):
        resp, _ = solve({"strip_width": 10,
                         "rectangles": [{"width": 2, "height": 2}]})
        self.assertNotIn("exact", resp)

    def test_response_is_json_serializable(self):
        resp, _ = solve({"strip_width": 10,
                         "rectangles": [{"width": 6, "height": 4}],
                         "compute_exact": True})
        s = json.dumps(resp, ensure_ascii=False)
        again = json.loads(s)
        self.assertEqual(again["status"], "ok")


class TestHeuristicNotOptimal(unittest.TestCase):
    """记录启发式与真实最优的关系：搜索小算例，证明存在差距，并断言永远不会反超。"""

    def test_heuristic_never_better_than_optimum_and_gap_exists(self):
        rng = np.random.default_rng(31337)
        gap_cases = []
        tested = 0
        for _ in range(200):
            n = int(rng.integers(3, 9))
            sw = int(rng.integers(4, 9))
            rects = [(float(rng.integers(1, sw + 1)),
                      float(rng.integers(1, 7))) for _ in range(n)]
            inst = validate_request(
                {"strip_width": sw,
                 "rectangles": [{"width": w, "height": h} for w, h in rects]})
            ex = solve_exact(inst)
            if ex.status != "optimal":
                continue
            tested += 1
            heur = best_heuristic(inst)
            # 不变量：启发式（可行解）不可能比真实最优更矮
            self.assertGreaterEqual(
                heur.height, ex.height - 1e-9,
                "W=%s rects=%s heur=%g opt=%g" % (sw, rects, heur.height, ex.height))
            if heur.height > ex.height + 1e-9:
                gap_cases.append((sw, rects, heur.height, ex.height))
        self.assertGreaterEqual(tested, 30, "实际参与比较的算例太少")
        # BL/FFDH 并非完美：200 个随机小算例中应能观察到至少一个非最优案例，
        # 用以实证"启发式不保证最优"这一声明。
        self.assertGreater(
            len(gap_cases), 0,
            "未找到启发式非最优案例——测试随机性可能已变化，请检查",
        )
        # 固定记录一个找到的案例，供 README 引用
        sw, rects, hh, oh = gap_cases[0]
        self.assertGreater(hh, oh)


class TestCLISmoke(unittest.TestCase):
    """CLI 冒烟测试：通过 stdin 喂请求，检查退出码与输出。"""

    def test_cli_ok(self):
        req = json.dumps({"strip_width": 10,
                          "rectangles": [{"width": 3, "height": 4}]})
        p = subprocess.run(
            [sys.executable, "-m", "packing.cli"],
            input=req, capture_output=True, text=True, cwd=ROOT,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        resp = json.loads(p.stdout)
        self.assertEqual(resp["status"], "ok")

    def test_cli_rejects_zero_size_with_code_1(self):
        req = json.dumps({"strip_width": 10,
                          "rectangles": [{"width": 0, "height": 4}]})
        p = subprocess.run(
            [sys.executable, "-m", "packing.cli"],
            input=req, capture_output=True, text=True, cwd=ROOT,
        )
        self.assertEqual(p.returncode, 1, p.stderr)
        resp = json.loads(p.stdout)
        self.assertEqual(resp["status"], "error")

    def test_cli_invalid_json_exit_3(self):
        p = subprocess.run(
            [sys.executable, "-m", "packing.cli"],
            input="{not json", capture_output=True, text=True, cwd=ROOT,
        )
        self.assertEqual(p.returncode, 3)


if __name__ == "__main__":
    unittest.main()
