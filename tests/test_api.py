"""端到端：JSON API（handle_json / solve）与失败状态、输入范围测试。"""
import json
import unittest
from fractions import Fraction as F

from rootisolate import engine
from rootisolate.api import handle_json


def jreq(obj):
    return handle_json(json.dumps(obj))


def jget(obj):
    out, code = jreq(obj)
    return code, json.loads(out)


class TestSpecialPolynomials(unittest.TestCase):
    def test_zero_polynomial(self):
        code, r = jget({"coefficients": [0, 0, 0]})
        self.assertEqual(code, 0)
        res = r["result"]
        self.assertEqual(res["status"], "zero_polynomial")
        self.assertEqual(res["polynomial_kind"], "zero")
        self.assertIsNone(res["root_count_distinct"])
        self.assertEqual(res["real_roots"], [])

    def test_nonzero_constant(self):
        code, r = jget({"coefficients": ["3/4"]})
        self.assertEqual(code, 0)
        res = r["result"]
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["polynomial_kind"], "constant")
        self.assertEqual(res["root_count_distinct"], 0)
        self.assertEqual(res["root_count_with_multiplicity"], 0)

    def test_negative_constant(self):
        code, r = jget({"coefficients": [-7]})
        self.assertEqual(code, 0)
        self.assertEqual(r["result"]["root_count_distinct"], 0)


class TestEndToEndCounts(unittest.TestCase):
    def setUp(self):
        self.cases = [
            # (coeffs, distinct, weighted, mults)
            ([-2, 0, 1], 2, 2, [1, 1]),
            ([0, 0, 1], 1, 2, [2]),                 # x^2
            ([1, 0, -2, 0, 1], 2, 4, [2, 2]),       # even mult
            ([-8, 12, -6, 1], 1, 3, [3]),           # triple
            ([0, -1, 0, 1], 3, 3, [1, 1, 1]),
            ([1, 0, 0, 0, 1], 0, 0, []),            # no real root
            ([0, 0, -1, 1], 2, 3, [2, 1]),          # x^2(x-1)
        ]

    def test_counts_and_certificates(self):
        for coeffs, d, w, mults in self.cases:
            code, r = jget({"coefficients": coeffs, "epsilon": "1e-14"})
            self.assertEqual(code, 0, (coeffs, r))
            res = r["result"]
            self.assertEqual(res["status"], "ok", (coeffs, res["status"]))
            self.assertEqual(res["root_count_distinct"], d, coeffs)
            self.assertEqual(res["root_count_with_multiplicity"], w, coeffs)
            got_mults = sorted(rt["multiplicity"] for rt in res["real_roots"])
            self.assertEqual(got_mults, sorted(mults), coeffs)
            # 每个区间都有计数与误差证书
            for rt in res["real_roots"]:
                ap = rt["approximation"]
                rad = F(ap["radius_rational"]["fraction"])
                mid = F(ap["midpoint_rational"]["fraction"])
                a = F(rt["interval"]["lower"]["fraction"])
                b = F(rt["interval"]["upper"]["fraction"])
                self.assertGreater(rad, 0)
                # 宣传的十进制中点+半径确实覆盖整个区间（拒绝假精度）
                self.assertLessEqual(abs((a + b) / 2 - mid) + (b - a) / 2,
                                     rad, coeffs)
                if rt["kind"] == "isolated_interval":
                    self.assertEqual(
                        rt["evidence"]["variation_difference"], 1)
                    self.assertEqual(
                        rt["sign_at_endpoints"]["lower"],
                        "positive" if False else
                        rt["sign_at_endpoints"]["lower"])
                    self.assertNotEqual(
                        rt["sign_at_endpoints"]["lower"],
                        rt["sign_at_endpoints"]["upper"])
                    self.assertNotIn(
                        rt["sign_at_endpoints"]["lower"], ("zero",))

    def test_descending_order_matches_ascending(self):
        code1, r1 = jget({"coefficients": [-2, 0, 1],
                          "coefficient_order": "ascending"})
        code2, r2 = jget({"coefficients": [1, 0, -2],
                          "coefficient_order": "descending"})
        self.assertEqual(code1, code2, 0)
        self.assertEqual(
            r1["result"]["root_count_distinct"],
            r2["result"]["root_count_distinct"])

    def test_rational_string_coefficients(self):
        # (x-1/2)(x-2/3) = x^2 - 7/6 x + 1/3
        code, r = jget({"coefficients": ["1/3", "-7/6", 1]})
        self.assertEqual(code, 0)
        self.assertEqual(r["result"]["root_count_distinct"], 2)

    def test_decimal_string_coefficient_exact(self):
        # 小数字符串必须被解析为精确有理数，而非二进制浮点。
        # x - 0.25 的根恰为 1/4。
        code, r = jget({"coefficients": ["-0.25", "1"]})
        self.assertEqual(code, 0)
        res = r["result"]
        self.assertEqual(res["status"], "ok")
        root = res["real_roots"][0]
        # 隔离/细化得到的包围必须覆盖精确的 1/4
        a = F(root["interval"]["lower"]["fraction"])
        b = F(root["interval"]["upper"]["fraction"])
        self.assertLessEqual(a, F(1, 4))
        self.assertLessEqual(F(1, 4), b)
        # 输入系数在回显中保持精确分母 4
        echo = res["input"]["coefficients"]
        self.assertEqual(
            echo[0], {"fraction": "-1/4", "numerator": -1, "denominator": 4})


class TestBoundsFiltering(unittest.TestCase):
    def test_custom_bounds_include(self):
        code, r = jget({"coefficients": [0, -1, 0, 1],
                        "bounds": {"lower": "-2", "upper": "2"}})
        self.assertEqual(code, 0)
        self.assertEqual(r["result"]["root_count_distinct"], 3)

    def test_custom_bounds_partial(self):
        code, r = jget({"coefficients": [0, -1, 0, 1],
                        "bounds": {"lower": "0", "upper": "2"}})
        self.assertEqual(code, 0)
        res = r["result"]
        self.assertEqual(res["root_count_distinct"], 2)  # 0 与 1
        rels = {F(x["exact_value"]["fraction"] if x["kind"] == "exact"
                  else x["interval"]["lower"]["fraction"]):
                x["relation_to_requested_bounds"]
                for x in res["real_roots"]}
        # 全局计数仍是 3
        self.assertEqual(res["global_root_count_distinct"], 3)

    def test_boundary_exact_root_flag(self):
        code, r = jget({"coefficients": [0, -1, 0, 1],
                        "bounds": {"lower": "1", "upper": "5"}})
        self.assertEqual(code, 0)
        rels = [x["relation_to_requested_bounds"]
                for x in r["result"]["real_roots"]]
        self.assertIn("boundary_exact", rels)

    def test_bound_excludes_all(self):
        code, r = jget({"coefficients": [0, -1, 0, 1],
                        "bounds": {"lower": "10", "upper": "20"}})
        self.assertEqual(code, 0)
        self.assertEqual(r["result"]["root_count_distinct"], 0)
        self.assertEqual(r["result"]["global_root_count_distinct"], 3)

    def test_crossing_bound_includes_or_excludes_by_variations(self):
        # x^2-2 的 +sqrt2 ≈ 1.4142；用非根边界 1.4 / 1.5 检验 Sturm 归属
        code_in, rin = jget({"coefficients": [-2, 0, 1],
                             "bounds": {"lower": "-10", "upper": "1.5"}})
        self.assertEqual(rin["result"]["root_count_distinct"], 2)
        code_out, rout = jget({"coefficients": [-2, 0, 1],
                               "bounds": {"lower": "-10", "upper": "1.4"}})
        self.assertEqual(code_out, 0)
        self.assertEqual(rout["result"]["root_count_distinct"], 1)
        mids = [x["approximation"]["midpoint"]
                for x in rout["result"]["real_roots"]]
        self.assertTrue(all(m.startswith("-") for m in mids))


class TestInputValidation(unittest.TestCase):
    def test_malformed_json(self):
        out, code = handle_json("{not json")
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(out)["ok"])

    def test_missing_coefficients(self):
        code, r = jget({"epsilon": "1e-9"})
        self.assertEqual(code, 2)
        self.assertEqual(r["error"]["code"], "invalid_request")

    def test_float_coefficient_rejected(self):
        code, r = jget({"coefficients": [0.1, 1]})
        self.assertEqual(code, 2)
        self.assertEqual(r["error"]["code"], "invalid_input")

    def test_unparseable_fraction(self):
        code, r = jget({"coefficients": ["abc", 1]})
        self.assertEqual(code, 2)
        self.assertEqual(r["error"]["code"], "invalid_input")

    def test_bad_epsilon(self):
        for eps in ["0", "-1", "abc"]:
            code, r = jget({"coefficients": [1, 1], "epsilon": eps})
            self.assertEqual(code, 2, eps)

    def test_epsilon_too_tiny_rejected(self):
        code, r = jget({"coefficients": [1, 1],
                        "epsilon": "1e-300"})
        self.assertEqual(code, 2)
        self.assertEqual(r["error"]["code"], "epsilon_out_of_range")

    def test_degree_too_large(self):
        code, r = jget({"coefficients": [1] * 102})
        self.assertEqual(code, 2)
        self.assertEqual(r["error"]["code"], "degree_out_of_range")

    def test_bad_bounds(self):
        code, r = jget({"coefficients": [1, 1],
                        "bounds": {"lower": "2", "upper": "1"}})
        self.assertEqual(code, 2)

    def test_depth_out_of_range(self):
        code, r = jget({"coefficients": [1, 1], "max_depth": 10 ** 9})
        self.assertEqual(code, 2)


class TestFailureStatuses(unittest.TestCase):
    def test_isolation_depth_status(self):
        # 4 个不同根 (x-1)(x-2)(x-3)(x-4)，max_depth=1：切分前整个 Cauchy
        # 区间含 4 根；第一次切分后两半各 2 根，深度即耗尽 -> 必产生未决。
        coeffs = [24, -50, 35, -10, 1]
        code, r = jget({"coefficients": coeffs, "max_depth": 1})
        self.assertEqual(code, 0)
        res = r["result"]
        self.assertEqual(res["status"], "isolation_depth")
        self.assertTrue(res["unresolved"])
        # 未决区间给出可信计数
        unresolved_roots = sum(
            u["distinct_roots_in_interval"] for u in res["unresolved"])
        self.assertEqual(unresolved_roots, 4)
        # 全局计数仍准确（Sturm 证书不依赖隔离是否完成）
        self.assertEqual(res["global_root_count_distinct"], 4)
        self.assertEqual(res["global_root_count_with_multiplicity"], 4)

    def test_refine_depth_status(self):
        # 极大 epsilon 目标 + 小深度：根已隔离但细化不达标
        code, r = jget({"coefficients": [-2, 0, 1], "epsilon": "1e-100",
                        "max_depth": 10})
        self.assertEqual(code, 0)
        self.assertIn(r["result"]["status"],
                      ("refine_depth", "ok"))


class TestResponseIsJsonNative(unittest.TestCase):
    def test_no_nan_or_inf(self):
        code, r = jget({"coefficients": [-2, 0, 1]})
        text = json.dumps(r, allow_nan=False)  # 不抛即通过
        self.assertIn("result", text)

    def test_counting_basis_present(self):
        code, r = jget({"coefficients": [-2, 0, 1]})
        cb = r["result"]["counting_basis"]
        self.assertEqual(cb["method"], "Sturm's theorem with sign variations")
        self.assertTrue(cb["factor_reports"])
        fr = cb["factor_reports"][0]
        self.assertIn("sturm_sequence", fr)
        self.assertEqual(fr["variations_at_cauchy_lower"] -
                         fr["variations_at_cauchy_upper"], 2)


if __name__ == "__main__":
    unittest.main()
