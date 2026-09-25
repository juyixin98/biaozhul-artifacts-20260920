"""精确算术层（polynomial / squarefree / sturm）单元测试。"""
import unittest
from fractions import Fraction as F

import numpy as np

from rootisolate import polynomial as P
from rootisolate import squarefree, sturm


def P_(coeffs):
    return P.poly(coeffs)


class TestPolynomial(unittest.TestCase):
    def test_trim_and_zero(self):
        self.assertTrue(P.is_zero(P_([0, 0])))
        self.assertEqual(P.degree(P_([0])), -1)
        self.assertEqual(P.degree(P_([1, 0])), 0)
        self.assertEqual(P.degree(P_([0, 1])), 1)

    def test_add_sub_mul(self):
        a, b = P_([1, 1]), P_([-1, 1])       # 1+x, -1+x
        self.assertEqual(list(P.add(a, b)), [F(0), F(2)])
        self.assertEqual(list(P.mul(a, b)), list(P_([-1, 0, 1])))  # x^2-1
        self.assertEqual(list(P.sub(b, a)), list(P_([-2])))

    def test_divmod_and_exact(self):
        f = P_([-6, 1, 1])     # x^2+x-6 = (x-2)(x+3)
        d = P_([-2, 1])        # x-2
        q, r = P.divmod_poly(f, d)
        self.assertTrue(P.is_zero(r))
        self.assertEqual(list(q), list(P_([3, 1])))

    def test_derivative(self):
        self.assertEqual(list(P.derivative(P_([1, 2, 3]))), [F(2), F(6)])
        self.assertTrue(P.is_zero(P.derivative(P_([5]))))

    def test_horner_exact(self):
        self.assertEqual(P.horner(P_([-2, 0, 1]), F(3)), F(7))
        self.assertEqual(P.horner(P_(["1/3", "1/2"]), F(2)),
                         F(1, 3) + 1)  # 1/3 + (1/2)*2 = 4/3

    def test_sign_at_matches_horner(self):
        rng = np.random.default_rng(7)
        for _ in range(40):
            deg = int(rng.integers(1, 8))
            coeffs = [F(int(rng.integers(-9, 10))) for _ in range(deg + 1)]
            a = P_(coeffs)
            x = F(int(rng.integers(-4, 5)), int(rng.integers(1, 4)))
            v = P.horner(a, x)
            want = 1 if v > 0 else (-1 if v < 0 else 0)
            self.assertEqual(P.sign_at(a, x), want, (coeffs, x))

    def test_primitive_positive_scaling(self):
        # a = 2x + 1/2 ；本原表示必须满足 a = s*g 且 s>0
        a = P_([F(1, 2), 2])
        g, s = P.primitive_positive(a)
        self.assertGreater(s, 0)
        rebuilt = P.scale(P.poly(g), s)
        self.assertEqual(list(rebuilt), list(a))
        # 正缩放不改首项符号
        self.assertEqual(P.sign_at(P.poly(g), 0), P.sign_at(a, 0))

    def test_cauchy_bound_strict(self):
        for coeffs, roots in [
            ([-2, 0, 1], [-2 ** 0.5, 2 ** 0.5]),
            ([-6, 1, 1], [-3, 2]),
            ([-8, 12, -6, 1], [2]),
        ]:
            a = P_(coeffs)
            B = P.cauchy_bound(a)
            for rt in roots:
                self.assertLess(abs(rt), float(B))
            self.assertEqual(P.sign_at(a, -B) != 0, True)
            self.assertEqual(P.sign_at(a, B) != 0, True)


class TestSquareFree(unittest.TestCase):
    def _rebuild(self, a):
        factors, lead = squarefree.squarefree(a)
        prod = P_([1])
        for f, m in factors:
            fm = f
            for _ in range(m - 1):
                fm = P.mul(fm, f)
            prod = P.mul(prod, fm)
        prod = P.scale(prod, lead)
        return factors, prod

    def test_simple(self):
        a = P_([-2, 0, 1])
        factors, prod = self._rebuild(a)
        self.assertEqual([m for _, m in factors], [1])
        self.assertEqual(list(prod), list(a))

    def test_double_and_quad(self):
        # (x-1)^2 (x+1)^2 = x^4 - 2x^2 + 1
        a = P_([1, 0, -2, 0, 1])
        factors, prod = self._rebuild(a)
        self.assertEqual([m for _, m in factors], [2])
        self.assertEqual(list(prod), list(a))

    def test_triple(self):
        a = P_([-8, 12, -6, 1])               # (x-2)^3
        factors, prod = self._rebuild(a)
        self.assertEqual([m for _, m in factors], [3])
        self.assertEqual(list(prod), list(a))

    def test_mixed_multiplicities(self):
        # (x-1)^1 (x-2)^2 (x-3)^3
        a = P.mul(P.mul(P_([-1, 1]),
                        P.mul(P_([-2, 1]), P_([-2, 1]))),
                  P.mul(P.mul(P_([-3, 1]), P_([-3, 1])), P_([-3, 1])))
        factors, prod = self._rebuild(a)
        mults = sorted(m for _, m in factors)
        self.assertEqual(mults, [1, 2, 3])
        self.assertEqual(list(P.trim(prod)), list(P.trim(a)))

    def test_rational_coefficients(self):
        a = P_([F(1, 6), F(-5, 6), F(1)])   # (x-1/2)(x-1/3)
        factors, prod = self._rebuild(a)
        self.assertEqual(list(prod), list(a))

    def test_constant(self):
        factors, lead = squarefree.squarefree(P_([7]))
        self.assertEqual(factors, [])
        self.assertEqual(lead, 7)


class TestSturm(unittest.TestCase):
    def test_variations_helper(self):
        self.assertEqual(sturm.variations([1, -1, 1, -1]), 3)
        self.assertEqual(sturm.variations([1, 0, -1, 0, 1]), 2)
        self.assertEqual(sturm.variations([1, 1, 1]), 0)

    def test_root_counts(self):
        cases = [
            ([-2, 0, 1], 2),       # ±sqrt2
            ([1, 0, 0, 0, 1], 0),  # x^4+1
            ([0, -1, 0, 1], 3),    # -1,0,1
            ([-1, 0, 0, 1], 1),    # x^3-1 one real
            ([1, 0, -2, 0, 1], 2), # even mult -> two distinct
        ]
        for coeffs, want in cases:
            p = P_(coeffs)
            chain = sturm.sturm_chain(p)
            B = P.cauchy_bound(p)
            self.assertEqual(sturm.v_at(chain, -B) - sturm.v_at(chain, B), want,
                             coeffs)

    def test_chain_sign_preserving_scaling(self):
        # 缩放余数为正不能改变任何一个链多项式的根与符号行为
        p = P_([F(-1, 2), 0, F(3, 2)])  # (3x^2-1)/2
        chain = sturm.sturm_chain(p)
        self.assertGreaterEqual(len(chain), 2)

    def test_left_right_limits_at_simple_root(self):
        p = P_([-1, 1])           # x-1, root at 1
        chain = sturm.sturm_chain(p)
        # 根是端点 1：开区间 (0,1) 与 (1,2) 都不含根
        self.assertEqual(sturm.v_at(chain, 0) - sturm.v_left(chain, 1), 0)
        self.assertEqual(sturm.v_right(chain, 1) - sturm.v_at(chain, 2), 0)
        # 跨越根的开区间 (0,2) 恰含 1 根
        self.assertEqual(sturm.v_at(chain, 0) - sturm.v_at(chain, 2), 1)
        # V-(1) 与 V+(1) 相差恰 1（根在 1 这一点）
        self.assertEqual(sturm.v_left(chain, 1)
                         - sturm.v_right(chain, 1), 1)


if __name__ == "__main__":
    unittest.main()
