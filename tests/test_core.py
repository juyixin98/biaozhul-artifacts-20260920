"""核心算法单元测试：多项式运算、无平方因子分解、Sturm 计数、隔离与细化。

运行：``python -m unittest discover -s tests -v`` 或 ``python -m pytest``
"""

from __future__ import annotations

import unittest
from fractions import Fraction

from rootisolation.isolation import (
    RefinementLimitError,
    cauchy_bound,
    isolate_real_roots,
    refine_interval,
    target_width_for_digits,
)
from rootisolation.polyops import (
    degree,
    derivative,
    evaluate,
    is_zero,
    poly_divmod,
    poly_gcd,
    poly_mul,
    rational_linear_roots,
    sign_variations,
    square_free_factorization,
    sturm_sequence,
    trim,
)
from tests.helpers import poly_from_roots


F = Fraction


def qpoly(coeffs):
    return [F(x) if not isinstance(x, Fraction) else x for x in coeffs]


class TestPolyArithmetic(unittest.TestCase):
    def test_divmod(self):
        # (x^2 - 1) / (x - 1) = x + 1, rem 0
        q, r = poly_divmod([-1, 0, 1], [-1, 1])
        self.assertEqual(q, [1, 1])
        self.assertTrue(is_zero(r))

    def test_divmod_with_remainder(self):
        # (x^2+1)/(x) -> q=x, r=1
        q, r = poly_divmod([1, 0, 1], [0, 1])
        self.assertEqual(q, [0, 1])
        self.assertEqual(r, [1])

    def test_gcd_double_root(self):
        # (x-1)^2 (x+2) 与导数的 gcd 含 (x-1)
        p = poly_mul(poly_mul([-1, 1], [-1, 1]), [2, 1])
        g = poly_gcd(p, derivative(p))
        self.assertEqual(degree(g), 1)
        self.assertEqual(evaluate(g, 1), 0)

    def test_gcd_coprime(self):
        p = poly_mul([0, 1], [-1, 1])          # x(x-1)
        g = poly_gcd(p, derivative(p))
        self.assertEqual(degree(g), 0)


class TestSquareFree(unittest.TestCase):
    def _check(self, coeffs, expected_factor_mult):
        p = qpoly(coeffs)
        fac = square_free_factorization(p)
        got = sorted((degree(g), m) for g, m in fac)
        self.assertEqual(got, sorted(expected_factor_mult))
        # 恒等检验：lc(p) · ∏ g^m == p
        prod = [F(1)]
        for g, m in fac:
            for _ in range(m):
                prod = poly_mul(prod, g)
        lc = p[-1]
        prod = [c * lc for c in prod]
        self.assertEqual(trim(prod), trim(p))

    def test_simple(self):
        self._check([-1, 0, 1], [(2, 1)])           # x^2-1 无重根

    def test_double(self):
        self._check([1, -2, 1], [(1, 2)])          # (x-1)^2

    def test_triple(self):
        self._check([0, 0, 0, 1], [(1, 3)])        # x^3

    def test_mixed_multiplicities(self):
        # (x-1)^2 (x-3)^3
        p = poly_mul(poly_from_roots([1, 1]), poly_from_roots([3, 3, 3]))
        fac = square_free_factorization(p)
        d = {m: degree(g) for g, m in fac}
        self.assertEqual(d.get(2), 1)
        self.assertEqual(d.get(3), 1)

    def test_even_multiple_roots(self):
        # (x^2-1)^2 = (x-1)^2 (x+1)^2
        p = poly_mul([-1, 0, 1], [-1, 0, 1])
        fac = square_free_factorization(p)
        d = {m: degree(g) for g, m in fac}
        self.assertEqual(d.get(2), 2)


class TestRationalRoots(unittest.TestCase):
    def test_integer_roots(self):
        # x^3-6x^2+11x-6 = (x-1)(x-2)(x-3)，低次到高次
        roots, rem = rational_linear_roots([F(-6), F(11), F(-6), F(1)])
        self.assertEqual(roots, [F(1), F(2), F(3)])
        self.assertEqual(degree(rem), 0)

    def test_fractional_and_integer(self):
        # (x-1/2)(x-2) = x^2 -(5/2)x + 1，低次到高次 [1, -5/2, 1]
        roots, rem = rational_linear_roots([F(1), F(-5, 2), F(1)])
        self.assertEqual(roots, [F(1, 2), F(2)])

    def test_root_zero(self):
        roots, rem = rational_linear_roots([F(0), F(-3), F(1)])  # x(x-3)
        self.assertEqual(roots, [F(0), F(3)])

    def test_irrational_left_alone(self):
        # x^2-2 无有理根：roots 为空，remainder 是原多项式
        roots, rem = rational_linear_roots([F(-2), F(0), F(1)])
        self.assertEqual(roots, [])
        self.assertEqual(degree(rem), 2)

    def test_mixed_rational_irrational(self):
        # (x-1)(x^2-2) = x^3 - x^2 - 2x + 2，低次到高次 [2,-2,-1,1]
        roots, rem = rational_linear_roots([F(2), F(-2), F(-1), F(1)])
        self.assertEqual(roots, [F(1)])
        self.assertEqual(degree(rem), 2)      # 剩余 x^2-2


class TestSturmConvention(unittest.TestCase):
    """用最简单情形确定 V(a)-V(b) 的端点约定：(a,b]。"""

    def test_linear_endpoint(self):
        seq = sturm_sequence([F(0), F(1)])          # p = x, 根 0
        # V(-1)-V(0) 含右端点根 0 -> 1
        self.assertEqual(sign_variations(seq, F(-1))
                         - sign_variations(seq, F(0)), 1)
        # V(0)-V(1) 不含左端点根 0 -> 0
        self.assertEqual(sign_variations(seq, F(0))
                         - sign_variations(seq, F(1)), 0)

    def test_strict_inner_via_subtraction(self):
        # p = x(x-1/2)(x-1)
        p = poly_from_roots([0, F(1, 2), 1])
        seq = sturm_sequence(p)
        # 严格 (0,1) 只有 1/2
        n = sign_variations(seq, F(0)) - sign_variations(seq, F(1))
        if evaluate(p, F(1)) == 0:
            n -= 1
        self.assertEqual(n, 1)


class TestCauchyBound(unittest.TestCase):
    def test_roots_inside(self):
        from tests.helpers import numpy_real_roots
        for coeffs in ([-2, 0, 1], [-6, 11, -6, 1], [1, 0, 1]):
            p = qpoly(coeffs)
            b = cauchy_bound(p)
            self.assertNotEqual(evaluate(p, b), 0)
            self.assertNotEqual(evaluate(p, -b), 0)
            for r in numpy_real_roots(coeffs):
                self.assertLess(abs(r), float(b))


class TestIsolation(unittest.TestCase):
    def assert_intervals_valid(self, p, expected_roots, digits_tol=None):
        """通用检验：区间数==不同根数；每区间内部恰一根；互不相交；覆盖全部根。

        expected_roots 为 Fraction（精确已知根）或 float（近似根）。
        精确已知根若不是二进中点能命中的（如 1/2、1/3），被非退化区间
        严格包围也算正确；0 这类会被精确命中为退化点。
        """
        p = qpoly(p)
        ivs = isolate_real_roots(p)
        exact_known = sorted(Fraction(r) for r in expected_roots
                             if isinstance(r, (Fraction, int)))
        approx = sorted(float(r) for r in expected_roots
                        if not isinstance(r, (Fraction, int)))

        # 1) 数量：每个不同根恰一个区间
        self.assertEqual(len(ivs), len(expected_roots))

        seq = sturm_sequence(p)
        # 2) 每个区间的性质
        for a, b in ivs:
            if a == b:
                self.assertEqual(evaluate(p, a), 0)       # 退化点必是根
                continue
            self.assertNotEqual(evaluate(p, a), 0)        # 端点干净
            self.assertNotEqual(evaluate(p, b), 0)
            inner = sign_variations(seq, a) - sign_variations(seq, b)
            if evaluate(p, b) == 0:
                inner -= 1
            self.assertEqual(inner, 1, f"区间 ({a},{b}) 内部根数不为 1")

        # 3) 区间互不相交（闭包也不重叠；精确点与邻居区间可共享坐标但不重复根）
        boxes = sorted((a, b) for a, b in ivs)
        for (a1, b1), (a2, b2) in zip(boxes, boxes[1:]):
            self.assertLessEqual(b1, a2, "隔离区间相互重叠")

        # 4) 每个精确已知根要么是退化点，要么严格落在某个非退化区间内
        for r in exact_known:
            hit = any(a == b == r for a, b in ivs) or \
                any(a < r < b for a, b in ivs if a != b)
            self.assertTrue(hit, f"精确根 {r} 未被隔离结果覆盖")

        # 5) 每个退化点必须是期望根之一
        exact_got = {a for a, b in ivs if a == b}
        for x in exact_got:
            self.assertIn(x, exact_known, f"意外的精确点 {x}")

        # 6) 近似根落在某个非退化区间内
        for r in approx:
            self.assertTrue(
                any(a < r < b for a, b in ivs if a != b),
                f"近似根 {r} 未被任何区间包围",
            )

    def test_simple_two_roots(self):
        self.assert_intervals_valid([-1, 0, 1], [-1, 1])

    def test_adjacent_roots(self):
        p = poly_from_roots([0, F(1, 2), 1])
        self.assert_intervals_valid(p, [F(0), F(1, 2), F(1)])

    def test_very_close_integer_roots(self):
        # x(x-1)(x-2)：相邻整根，检验端点为根时不丢根/不重根
        p = poly_from_roots([0, 1, 2])
        self.assert_intervals_valid(p, [F(0), F(1), F(2)])

    def test_double_root(self):
        self.assert_intervals_valid([1, -2, 1], [F(1)])

    def test_even_double_two_roots(self):
        p = poly_mul([-1, 0, 1], [-1, 0, 1])    # (x^2-1)^2
        self.assert_intervals_valid(p, [F(-1), F(1)])

    def test_triple_root(self):
        self.assert_intervals_valid([0, 0, 0, 1], [F(0)])

    def test_irrational_roots(self):
        import math
        self.assert_intervals_valid([-2, 0, 1],
                                    [-math.sqrt(2), math.sqrt(2)])

    def test_no_real_roots(self):
        self.assertEqual(isolate_real_roots(qpoly([1, 0, 1])), [])

    def test_linear_irrational(self):
        # 2x - 1 -> 精确根 1/2
        self.assert_intervals_valid([-1, 2], [F(1, 2)])

    def test_wilkinson_small(self):
        # ∏(x-i), i=0..5
        p = poly_from_roots(range(6))
        self.assert_intervals_valid(p, [F(i) for i in range(6)])


class TestRefinement(unittest.TestCase):
    def test_refine_irrational(self):
        import math
        p = qpoly([-2, 0, 1])
        ivs = isolate_real_roots(p)
        nondeg = [(a, b) for a, b in ivs if a != b]
        self.assertEqual(len(nondeg), 2)
        tw = target_width_for_digits(10)
        for a, b in nondeg:
            a2, b2, it = refine_interval(p, a, b, tw)
            self.assertLessEqual(b2 - a2, tw)
            mid = float((a2 + b2) / 2)
            self.assertAlmostEqual(abs(mid), math.sqrt(2), delta=1e-9)

    def test_refine_exact_hit(self):
        p = qpoly([-1, 1])                       # x-1
        (a, b), = isolate_real_roots(p)
        a2, b2, it = refine_interval(p, a, b, F(1, 10 ** 20))
        self.assertEqual(a2, b2)
        self.assertEqual(a2, 1)

    def test_refinement_limit(self):
        # 极小的目标宽度 + 极少迭代必然失败，必须显式报错而非伪造精度
        p = qpoly([-2, 0, 1])
        pos = [(a, b) for a, b in isolate_real_roots(p) if a != b and b > 0]
        self.assertEqual(len(pos), 1)
        a, b = pos[0]
        with self.assertRaises(RefinementLimitError):
            refine_interval(p, a, b, F(1, 10 ** 100), max_iterations=3)


if __name__ == "__main__":
    unittest.main()
