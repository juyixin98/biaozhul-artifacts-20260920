"""核心求积算法测试：精度、误差预算、失败语义与输入范围。"""

import math
import unittest

from adaptive_integration import integrate


class TestSmoothAccuracy(unittest.TestCase):
    def test_polynomial_simpson_exact(self):
        # Simpson 对不超过三次的多项式精确
        r = integrate(lambda x: x ** 3, 0.0, 1.0, 1e-12, 1e-12)
        self.assertTrue(r.converged)
        self.assertEqual(r.value, 0.25)
        self.assertLessEqual(r.error_estimate, 1e-12 + 1e-12 * 0.25)

    def test_exp_matches_known_value(self):
        r = integrate(math.exp, 0.0, 1.0, 1e-12, 1e-12)
        self.assertTrue(r.converged)
        exact = math.e - 1.0
        self.assertLess(abs(r.value - exact), 1e-11)
        self.assertGreaterEqual(r.error_estimate, abs(r.value - exact) * 0.5)

    def test_gauss_method_smooth(self):
        r = integrate(math.exp, 0.0, 1.0, 1e-12, 1e-12, method="gauss")
        self.assertTrue(r.converged)
        self.assertLess(abs(r.value - (math.e - 1.0)), 1e-11)

    def test_reversed_interval_changes_sign(self):
        r_fwd = integrate(lambda x: x * math.exp(-x), 0.0, 2.0, 1e-10, 1e-10)
        r_rev = integrate(lambda x: x * math.exp(-x), 2.0, 0.0, 1e-10, 1e-10)
        self.assertTrue(r_fwd.converged and r_rev.converged)
        self.assertAlmostEqual(r_fwd.value, -r_rev.value, places=12)

    def test_degenerate_interval(self):
        r = integrate(math.exp, 1.0, 1.0, 1e-10, 1e-10)
        self.assertTrue(r.converged)
        self.assertEqual(r.value, 0.0)
        self.assertEqual(r.n_evals, 0)


class TestHighOscillation(unittest.TestCase):
    def test_moderate_oscillation_converges(self):
        # ∫₀¹ sin(2π·10·x) dx = 0
        r = integrate(lambda x: math.sin(2 * math.pi * 10 * x),
                      0.0, 1.0, 1e-8, 1e-8)
        self.assertTrue(r.converged)
        self.assertLess(abs(r.value), 1e-7)

    def test_very_high_oscillation_hits_budget(self):
        # 非整数倍频避免采样混叠；20000 个半周期在 100k 预算内无法整体分辨
        r = integrate(lambda x: math.sin(2 * math.pi * 10000.25 * x),
                      0.0, 1.0, 1e-8, 1e-8, max_evals=2000)
        self.assertFalse(r.converged)
        self.assertIsNone(r.value)
        self.assertEqual(r.error_code, "EVAL_BUDGET_REACHED")

    def test_aliasing_simpson_silently_wrong_gauss_saves(self):
        # 经典混叠：Simpson 全部采样点落在同相位置，误差估计给出极小值，
        # 但真实答案完全错误（真实积分=0）。这是误差估计失效的典型区间。
        f = lambda x: math.sin(16 * math.pi * x + 0.3)
        rs = integrate(f, 0.0, 1.0, 1e-10, 1e-10, method="simpson")
        rg = integrate(f, 0.0, 1.0, 1e-10, 1e-10, method="gauss")
        self.assertTrue(rs.converged)          # 声称收敛
        self.assertGreater(abs(rs.value), 1e-2)  # 实际值错误
        self.assertLess(rs.error_estimate, 1e-12)  # 误差估计还异常小
        self.assertTrue(rg.converged)
        self.assertLess(abs(rg.value), 1e-9)   # Gauss 正确识别振荡


class TestSingularities(unittest.TestCase):
    def test_endpoint_singularity_inverse_sqrt(self):
        r = integrate(lambda x: 1.0 / math.sqrt(x), 0.0, 1.0, 1e-8, 1e-8)
        self.assertFalse(r.converged)
        self.assertEqual(r.error_code, "SINGULAR_ENDPOINT")
        self.assertEqual(r.details["position"], "endpoint")
        self.assertIsNone(r.value)

    def test_endpoint_singularity_log(self):
        r = integrate(math.log, 0.0, 1.0, 1e-8, 1e-8)
        self.assertFalse(r.converged)
        self.assertEqual(r.error_code, "SINGULAR_ENDPOINT")

    def test_interior_pole(self):
        r = integrate(lambda x: 1.0 / (x - 0.5), 0.0, 1.0, 1e-8, 1e-8)
        self.assertFalse(r.converged)
        self.assertEqual(r.error_code, "SINGULAR_INTERIOR")

    def test_removable_singularity_rejected_without_help(self):
        # sin(x)/x 在 0 点可去奇点：库不做极限外推，要求用户显式定义
        r = integrate(lambda x: math.sin(x) / x if x != 0 else float("nan"),
                      0.0, 1.0, 1e-8, 1e-8)
        self.assertFalse(r.converged)
        self.assertEqual(r.error_code, "SINGULAR_ENDPOINT")
        # 用户显式补上极限值后正常
        r2 = integrate(lambda x: math.sin(x) / x if x != 0 else 1.0,
                       0.0, 1.0, 1e-10, 1e-10)
        self.assertTrue(r2.converged)
        si1 = 0.9460830703671830  # Si(1) = ∫₀¹ sin(x)/x dx
        self.assertLess(abs(r2.value - si1), 1e-9)

    def test_substitution_regularizes_inverse_sqrt(self):
        # x=t^2 代换后 ∫₀¹ x^{-1/2} e^{-x} dx = ∫₀¹ 2 e^{-t^2} dt
        r = integrate(lambda t: 2.0 * math.exp(-t * t),
                      0.0, 1.0, 1e-10, 1e-10)
        self.assertTrue(r.converged)
        self.assertLess(abs(r.value - math.sqrt(math.pi) * math.erf(1)), 1e-9)


class TestNarrowPeak(unittest.TestCase):
    @staticmethod
    def lorentz(eps):
        # 中心在 x=0.5 的归一化 Lorentz 峰；端点窄峰的误差估计盲区
        # 不在本测试中，由 scripts/acceptance_study.py 专门展示
        return lambda x: eps / (math.pi * ((x - 0.5) ** 2 + eps * eps))

    @staticmethod
    def exact(eps):
        return 2.0 * math.atan(0.5 / eps) / math.pi

    def test_wide_peak_converges(self):
        r = integrate(self.lorentz(1e-2), 0.0, 1.0, 1e-8, 1e-8)
        self.assertTrue(r.converged)
        self.assertLess(abs(r.value - self.exact(1e-2)), 1e-7)

    def test_tight_peak_depth_limit_then_success_deeper(self):
        r = integrate(self.lorentz(1e-8), 0.0, 1.0, 1e-8, 1e-8,
                      max_depth=20)
        self.assertFalse(r.converged)
        self.assertEqual(r.error_code, "MAX_DEPTH_REACHED")
        r2 = integrate(self.lorentz(1e-8), 0.0, 1.0, 1e-8, 1e-8,
                       max_depth=40)
        self.assertTrue(r2.converged)
        self.assertLess(abs(r2.value - self.exact(1e-8)), 1e-7)


class TestInputValidation(unittest.TestCase):
    def test_bad_method(self):
        with self.assertRaises(ValueError):
            integrate(math.exp, 0, 1, method="monte_carlo")

    def test_bad_tolerances(self):
        with self.assertRaises(ValueError):
            integrate(math.exp, 0, 1, eps_abs=-1e-9, eps_rel=1e-9)
        with self.assertRaises(ValueError):
            integrate(math.exp, 0, 1, eps_abs=0, eps_rel=0)

    def test_depth_and_budget_ranges(self):
        with self.assertRaises(ValueError):
            integrate(math.exp, 0, 1, max_depth=0)
        with self.assertRaises(ValueError):
            integrate(math.exp, 0, 1, max_depth=41)
        with self.assertRaises(ValueError):
            integrate(math.exp, 0, 1, max_evals=0)

    def test_budget_reached_small_cap(self):
        r = integrate(math.exp, 0.0, 1.0, 1e-12, 1e-12, max_evals=10)
        self.assertFalse(r.converged)
        self.assertEqual(r.error_code, "EVAL_BUDGET_REACHED")

    def test_non_numeric_return(self):
        r = integrate(lambda x: "x", 0.5, 1.5, 1e-8, 1e-8)
        self.assertFalse(r.converged)
        self.assertIn(r.error_code,
                      ("SINGULAR_INTERIOR", "SINGULAR_ENDPOINT"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
