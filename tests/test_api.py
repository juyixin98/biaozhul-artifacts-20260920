"""JSON API 层测试：状态、失败处理、输入校验、端到端 CLI、拒绝假精度。"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from fractions import Fraction

from rootisolation import solve
from rootisolation.api import (
    MAX_DEGREE,
    InvalidRequest,
    guaranteed_decimal_digits,
    solve_poly,
)
from rootisolation.__main__ import main as cli_main
from tests.helpers import numpy_real_roots, poly_from_roots


def roots_midpoints(resp):
    out = []
    for rt in resp["roots"]:
        a = Fraction(rt["interval"]["lower"])
        b = Fraction(rt["interval"]["upper"])
        out.append(((a + b) / 2, rt))
    return sorted(out, key=lambda t: float(t[0]))


class TestDegeneratePolynomials(unittest.TestCase):
    def test_zero_polynomial(self):
        r = solve({"coefficients": [0, 0, 0]})
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["kind"], "zero_polynomial")
        self.assertEqual(r["summary"]["distinct_real_roots"], 0)
        self.assertIn("无穷多", r["summary"]["root_count_basis"])
        self.assertEqual(r["roots"], [])

    def test_empty_coefficients(self):
        r = solve({"coefficients": []})
        self.assertEqual(r["kind"], "zero_polynomial")

    def test_constant_nonzero(self):
        r = solve({"coefficients": [7]})
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["kind"], "constant")
        self.assertEqual(r["summary"]["distinct_real_roots"], 0)
        self.assertEqual(r["roots"], [])

    def test_negative_constant(self):
        r = solve({"coefficients": ["-3/2"]})
        self.assertEqual(r["kind"], "constant")


class TestRootCountAndMultiplicity(unittest.TestCase):
    def test_counts_match_numpy(self):
        # 多个用例：互异实根数 / 含重数实根数 与 NumPy 独立结果交叉核对
        cases = [
            [-6, 11, -6, 1],                       # (x-1)(x-2)(x-3)
            [-2, 0, 1],                            # x^2-2
            [1, 0, 1],                             # x^2+1 无实根
        ]
        # 构造 (x-1)^2 (x-2)
        p = poly_from_roots([1, 1, 2])
        cases.append([str(c) for c in p])

        for c in cases:
            resp = solve({"coefficients": [str(x) for x in c]})
            np_roots = numpy_real_roots([Fraction(x) for x in c])
            self.assertEqual(
                resp["summary"]["distinct_real_roots"], len(np_roots),
                f"实根数与 NumPy 不符: {c}",
            )
            self.assertEqual(
                resp["summary"]["real_roots_with_multiplicity"]
                + resp["summary"]["nonreal_complex_roots_with_multiplicity"],
                resp["summary"]["degree"],
            )

    def test_multiplicity_even_root(self):
        # (x-2)^2 (x+1)：2 是偶重根，-1 单根
        p = poly_from_roots([2, 2, -1])
        r = solve({"coefficients": [str(c) for c in p],
                   "decimal_digits": 8})
        self.assertEqual(r["summary"]["distinct_real_roots"], 2)
        self.assertEqual(r["summary"]["real_roots_with_multiplicity"], 3)
        mults = {float((Fraction(x["interval"]["lower"])
                        + Fraction(x["interval"]["upper"])) / 2):
                 x["multiplicity"] for x in r["roots"]}
        # 找最接近 2 和 -1 的
        at2 = min(mults, key=lambda z: abs(z - 2))
        atm1 = min(mults, key=lambda z: abs(z + 1))
        self.assertAlmostEqual(at2, 2, places=5)
        self.assertAlmostEqual(atm1, -1, places=5)
        self.assertEqual(mults[at2], 2)
        self.assertEqual(mults[atm1], 1)


class TestIntervalGuarantees(unittest.TestCase):
    """验收核心：每个区间的边界与计数都要经得起检查。"""

    def _verify(self, coeffs, digits=12):
        resp = solve({"coefficients": [str(c) for c in coeffs],
                      "decimal_digits": digits})
        self.assertEqual(resp["status"], "ok")
        np_roots = numpy_real_roots([Fraction(c) for c in coeffs])
        roots = resp["roots"]
        self.assertEqual(len(roots), len(np_roots))

        prev_hi = None
        for rt, nr in zip(roots_midpoints(resp), sorted(np_roots)):
            mid, data = rt
            a = Fraction(data["interval"]["lower"])
            b = Fraction(data["interval"]["upper"])
            # 1) 数值中点与 NumPy 根一致
            self.assertAlmostEqual(float(mid), nr, delta=1e-6)
            # 2) 真实根严格位于精确有理区间内（精确点则相等）
            if a != b:
                self.assertLess(a, b)
                self.assertLess(abs(float(a) - nr), abs(float(b) - nr) + 1)
                self.assertTrue(float(a) < nr < float(b)
                                or data["interval"]["exact"])
            # 3) 区间宽度与声称的保证位数一致（不得假精度）。
            # 约定：宽度 <= 10^-d 即保证小数点后 d 位正确。
            if not data["interval"]["exact"]:
                width = b - a
                gdigits = data["decimal_enclosure"]["guaranteed_digits"]
                self.assertGreater(width, 0)
                self.assertLessEqual(width, Fraction(1, 10 ** gdigits))
                # d 是最大的这种整数：宽度 > 10^-(d+1)
                self.assertGreater(width, Fraction(1, 10 ** (gdigits + 1)))
                # 4) 小数有界显示确实夹住精确端点
                lo = data["decimal_enclosure"]["lower_bracket"]
                hi = data["decimal_enclosure"]["upper_bracket"]
                self.assertLessEqual(float(lo), float(a) + 1e-12)
                self.assertGreaterEqual(float(hi), float(b) - 1e-12)
            # 5) 区间不重叠
            if prev_hi is not None:
                self.assertLessEqual(float(prev_hi), float(a) + 1e-15)
            prev_hi = b
        return resp

    def test_quadratic(self):
        self._verify([-2, 0, 1])

    def test_cubic(self):
        self._verify([-6, 11, -6, 1])

    def test_quartic_with_repeat(self):
        p = poly_from_roots([1, 1, 2, 3])
        self._verify(p)

    def test_adjacent(self):
        p = poly_from_roots([0, Fraction(1, 2), 1])
        self._verify(p)


class TestFalsePrecision(unittest.TestCase):
    def test_guaranteed_digits_honest(self):
        # 高位数 + 极少迭代 -> refinement_limit，且保证位数如实下降
        r = solve({"coefficients": [-2, 0, 1],
                   "decimal_digits": 30,
                   "max_refinement_iterations": 5})
        self.assertEqual(r["status"], "refinement_limit")
        failed = r["errors"][0]["failed_intervals"]
        self.assertTrue(failed)
        for f in failed:
            # 5 次二分只能保证很少位数，绝不能声称 30
            self.assertLess(f["guaranteed_digits"], 10)
            self.assertGreater(Fraction(f["actual_width"]), 0)

    def test_width_matches_guarantee(self):
        # 宽度恰为 10^-d 时保证 d 位；略大则少一位
        self.assertEqual(guaranteed_decimal_digits(Fraction(1, 10 ** 7)), 7)
        self.assertEqual(guaranteed_decimal_digits(
            Fraction(2, 10 ** 7)), 6)


class TestInputValidation(unittest.TestCase):
    def test_bad_json_type(self):
        with self.assertRaises(InvalidRequest):
            solve_poly([1, 2, 3])

    def test_missing_coefficients(self):
        with self.assertRaises(InvalidRequest):
            solve_poly({})

    def test_boolean_coefficient_rejected(self):
        with self.assertRaises(InvalidRequest):
            solve({"coefficients": [True, 1]})

    def test_garbage_string_rejected(self):
        with self.assertRaises(InvalidRequest):
            solve({"coefficients": ["abc"]})

    def test_nan_rejected(self):
        with self.assertRaises(InvalidRequest):
            solve({"coefficients": [float("nan"), 1]})

    def test_degree_limit(self):
        coeffs = [1] + [0] * MAX_DEGREE + [1]   # degree MAX_DEGREE+1
        with self.assertRaises(InvalidRequest):
            solve({"coefficients": coeffs})

    def test_bad_digits(self):
        with self.assertRaises(InvalidRequest):
            solve({"coefficients": [-2, 0, 1], "decimal_digits": -1})

    def test_string_coefficient_exact_arithmetic(self):
        # "0.1" 必须被当作精确 1/10，而不是二进制浮点近似。
        # 多项式 (1/10)x - 1 有精确有理根 10，RRT 应识别为精确点。
        r = solve({"coefficients": ["-1", "0.1"], "decimal_digits": 10})
        (rt,) = r["roots"]
        self.assertTrue(rt["interval"]["exact"])
        self.assertEqual(rt["interval"]["lower"], "10")
        self.assertEqual(rt["evidence"]["method"], "rational_root_theorem")

    def test_decimal_string_not_float_artifact(self):
        # 0.1 若被错误当成浮点，系数会是很长的二进制分数；这里断言其精确等于 1/10
        from rootisolation.api import parse_coefficient
        self.assertEqual(parse_coefficient("0.1"), Fraction(1, 10))
        self.assertEqual(parse_coefficient("1e-3"), Fraction(1, 1000))
        self.assertEqual(parse_coefficient("1/7"), Fraction(1, 7))


class TestCLI(unittest.TestCase):
    def test_cli_stdin_ok(self):
        import sys
        old = sys.stdin
        sys.stdin = io.StringIO(json.dumps(
            {"coefficients": [-2, 0, 1], "decimal_digits": 4}))
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = cli_main([])
        finally:
            sys.stdin = old
        self.assertEqual(code, 0)
        resp = json.loads(buf.getvalue())
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(len(resp["roots"]), 2)

    def test_cli_invalid_exit_code(self):
        import sys
        old = sys.stdin
        sys.stdin = io.StringIO(json.dumps({"coefficients": ["nope"]}))
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = cli_main([])
        finally:
            sys.stdin = old
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(buf.getvalue())["status"],
                         "invalid_request")

    def test_cli_refinement_limit_exit_code(self):
        import sys
        old = sys.stdin
        sys.stdin = io.StringIO(json.dumps({
            "coefficients": [-2, 0, 1],
            "decimal_digits": 30,
            "max_refinement_iterations": 2,
        }))
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = cli_main([])
        finally:
            sys.stdin = old
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(buf.getvalue())["status"],
                         "refinement_limit")


if __name__ == "__main__":
    unittest.main()
