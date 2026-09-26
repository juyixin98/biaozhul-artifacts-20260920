"""二次方程解析求根测试：全部根均先手算再断言。"""

import unittest

from collision_detection.quadratic import solve_quadratic


class TestSolveQuadratic(unittest.TestCase):
    def test_two_distinct_roots(self):
        # x^2 - 5x + 6 = 0 => (x-2)(x-3) => 手算根 2, 3
        roots = solve_quadratic(1.0, -5.0, 6.0)
        self.assertEqual(len(roots), 2)
        self.assertAlmostEqual(roots[0], 2.0, places=12)
        self.assertAlmostEqual(roots[1], 3.0, places=12)

    def test_double_root_tangency(self):
        # x^2 - 2x + 1 = 0 => (x-1)^2 => 二重根 1（相切）
        roots = solve_quadratic(1.0, -2.0, 1.0)
        self.assertEqual(len(roots), 1)
        self.assertAlmostEqual(roots[0], 1.0, places=12)

    def test_no_real_roots(self):
        # x^2 + 1 = 0 => D = -4 < 0 => 无实根
        self.assertEqual(solve_quadratic(1.0, 0.0, 1.0), ())

    def test_negative_leading_coefficient(self):
        # -x^2 + 4 = 0 => 根 -2, 2，升序返回
        roots = solve_quadratic(-1.0, 0.0, 4.0)
        self.assertEqual(len(roots), 2)
        self.assertAlmostEqual(roots[0], -2.0, places=12)
        self.assertAlmostEqual(roots[1], 2.0, places=12)

    def test_stable_formula_preserves_small_root(self):
        # x^2 - 1e8 x + 1 = 0 => 手算根 ≈ 1e-8 与 1e8。
        # 朴素公式 (-b - sqrt(D))/2 中 sqrt(D)≈|b|，小根被抵消丢失；
        # 稳定 q 公式必须保住它。
        roots = solve_quadratic(1.0, -1e8, 1.0)
        self.assertEqual(len(roots), 2)
        self.assertAlmostEqual(roots[0] / 1e-8, 1.0, places=9)
        self.assertAlmostEqual(roots[1] / 1e8, 1.0, places=12)

    def test_rejects_linear_equation(self):
        with self.assertRaises(ValueError):
            solve_quadratic(0.0, 1.0, 1.0)


if __name__ == "__main__":
    unittest.main()
