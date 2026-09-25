"""Huber IRLS 核心性质测试。"""

import numpy as np
import unittest

from robust_regression import fit_huber, fit_ols, huber_weights


def outlier_data(seed: int = 1):
    """中心化 x 上的中部垂直离群点（4 个，符号交错）。

    构造方式固定（默认种子 1）：
    - x 在 [0,10] 上等距 30 点后中心化 -> 斜率与截距估计解耦
    - y = 2x + 1 + N(0, 0.3²)
    - 4 个中部点加入 ±8 的垂直离群（+,-,+,-）

    该配置下 OLS 斜率被明显拉偏（~1.83），δ=0.5 的 Huber
    几乎完全恢复真值，离群点权重 ~0.06、干净点权重 ~0.99。
    """
    rng = np.random.default_rng(seed)
    n = 30
    x = np.linspace(0.0, 10.0, n)
    x = x - x.mean()
    y = 2.0 * x + 1.0 + rng.normal(scale=0.3, size=n)
    idx = np.linspace(4, n - 5, 4).astype(int)
    y[idx] += 8.0 * np.array([1.0, -1.0, 1.0, -1.0])
    return x.reshape(-1, 1), y, idx


def scattered_outlier_data():
    """40 个随机点，4 个散布位置的 +12 离群点（固定生成过程）。

    离群点散布在中部/右端，OLS 初值残差的 MAD 不被污染，
    因此 δ="auto" 路径也能恢复真值。
    """
    rng = np.random.default_rng(0)
    x = rng.uniform(0.0, 10.0, size=40)
    y = 2.0 * x + 1.0 + rng.normal(scale=0.3, size=40)
    idx = np.sort(rng.choice(np.arange(3, 37), size=4, replace=False))
    y[idx] += 12.0
    return x.reshape(-1, 1), y, idx


class TestConvergenceAndDescent(unittest.TestCase):
    def test_converges_and_objective_nonincreasing(self):
        X, y, out_idx = outlier_data()
        res = fit_huber(X, y, delta=0.5, max_iter=300, tol=1e-10)
        self.assertEqual(res["status"], "converged")
        self.assertTrue(res["converged"])
        hist = np.array(res["objective_history"])
        self.assertTrue(
            np.all(np.diff(hist) <= 1e-10 * (1.0 + np.abs(hist[:-1]))),
            msg=f"目标值出现上升：{np.diff(hist).min()}",
        )
        self.assertAlmostEqual(res["objective"], hist[-1], places=12)

    def test_delta_large_matches_ols(self):
        rng = np.random.default_rng(7)
        X = rng.normal(size=(60, 2))
        y = (
            X @ np.array([1.0, -1.5])
            + 0.5
            + rng.normal(scale=0.1, size=60)
        )
        res = fit_huber(
            X, y, delta=1e6, reg_lambda=0.0, max_iter=300, tol=1e-12
        )
        ols = fit_ols(X, y, fit_intercept=True)
        np.testing.assert_allclose(
            res["coefficients"], ols["coefficients"], atol=1e-6
        )
        self.assertAlmostEqual(
            res["intercept"], ols["intercept"], delta=1e-6
        )

    def test_max_iterations_reached_status(self):
        X, y, out_idx = outlier_data()
        res = fit_huber(X, y, delta=0.5, max_iter=3, tol=1e-12)
        self.assertEqual(res["status"], "max_iterations_reached")
        self.assertFalse(res["converged"])
        self.assertEqual(res["iterations"], 3)
        # 即便没收敛，目标值相对 OLS 初值也不应上升
        self.assertLessEqual(
            res["objective"], res["objective_history"][0] + 1e-9
        )

    def test_huber_less_sensitive_to_outliers_than_ols(self):
        X, y, out_idx = outlier_data()
        ols = fit_ols(X, y)
        hub = fit_huber(X, y, delta=0.5, max_iter=300, tol=1e-10)
        # 真实斜率 2；OLS 被拉偏（约 1.83），Huber 明显更接近真值
        self.assertLess(abs(hub["coefficients"][0] - 2.0), 0.1)
        self.assertGreater(
            abs(ols["coefficients"][0] - 2.0),
            abs(hub["coefficients"][0] - 2.0),
        )
        self.assertLess(
            abs(hub["intercept"] - 1.0),
            abs(ols["intercept"] - 1.0),
        )
        # 离群点权重应被压低
        r = y - X[:, 0] * hub["coefficients"][0] - hub["intercept"]
        w = huber_weights(r, 0.5)
        self.assertLess(w[out_idx].mean(), 0.1)

    def test_reproducibility_bitwise(self):
        X, y, out_idx = outlier_data()
        r1 = fit_huber(X, y, delta=0.5, max_iter=300, tol=1e-10)
        r2 = fit_huber(X, y, delta=0.5, max_iter=300, tol=1e-10)
        self.assertEqual(
            r1["objective_history"], r2["objective_history"]
        )
        self.assertEqual(r1["coefficients"], r2["coefficients"])
        self.assertEqual(r1["intercept"], r2["intercept"])
        self.assertEqual(r1["iterations"], r2["iterations"])

    def test_auto_delta_robust_on_scattered_outliers(self):
        X, y, out_idx = scattered_outlier_data()
        res = fit_huber(X, y, delta="auto", max_iter=300, tol=1e-10)
        self.assertEqual(res["status"], "converged")
        self.assertIn("auto_scale_from_mad", res["warnings"])
        self.assertLess(abs(res["coefficients"][0] - 2.0), 0.1)
        self.assertLess(abs(res["intercept"] - 1.0), 0.1)


class TestEdgeCases(unittest.TestCase):
    def test_all_zero_residuals_perfect_fit(self):
        X = np.array([[1.0], [2.0], [3.0], [4.0]])
        y = 3.0 * X[:, 0] - 1.0
        res = fit_huber(X, y, delta="auto", max_iter=100, tol=1e-10)
        self.assertEqual(res["status"], "converged")
        self.assertIn("auto_scale_all_zero_residuals", res["warnings"])
        np.testing.assert_allclose(res["coefficients"], [3.0], atol=1e-9)
        self.assertAlmostEqual(res["intercept"], -1.0, places=9)
        self.assertAlmostEqual(res["objective"], 0.0, places=24)
        self.assertEqual(res["iterations"], 1)
        hist = res["objective_history"]
        self.assertEqual(len(hist), 2)
        for v in hist:
            self.assertAlmostEqual(v, 0.0, places=24)

    def test_collinear_columns_flagged_and_predictions_exact(self):
        x = np.linspace(0.0, 5.0, 20)
        X = np.column_stack([x, x, 2.0 * x])
        y = 4.0 * x + 0.5
        res = fit_huber(X, y, delta=1.0, max_iter=200, tol=1e-10)
        self.assertEqual(res["status"], "converged")
        self.assertTrue(res["rank_deficient"])
        self.assertEqual(res["rank"], 2)
        self.assertIn("rank_deficient_min_norm_solution", res["warnings"])
        pred = X @ np.array(res["coefficients"]) + res["intercept"]
        np.testing.assert_allclose(pred, y, atol=1e-7)

    def test_zero_feature_column(self):
        rng = np.random.default_rng(3)
        X = np.column_stack(
            [rng.normal(size=50), np.zeros(50)]
        )
        y = 1.0 + 0.8 * X[:, 0] + rng.normal(scale=0.01, size=50)
        res = fit_huber(X, y, delta=1.0, reg_lambda=0.0, max_iter=100)
        self.assertTrue(res["rank_deficient"])
        self.assertAlmostEqual(res["coefficients"][1], 0.0, places=9)
        self.assertAlmostEqual(res["coefficients"][0], 0.8, delta=0.02)

    def test_no_intercept_known_line(self):
        X = np.arange(1, 11, dtype=np.float64).reshape(-1, 1)
        y = -2.5 * X[:, 0]
        res = fit_huber(
            X, y, delta=1.0, fit_intercept=False, max_iter=100, tol=1e-10
        )
        self.assertEqual(res["status"], "converged")
        self.assertAlmostEqual(res["coefficients"][0], -2.5, places=7)
        self.assertEqual(res["intercept"], 0.0)

    def test_ridge_shrinks_coef_but_not_penalize_intercept(self):
        # 中心化 X：岭收缩斜率时，截距（不惩罚）仍应等于 y 均值 50
        rng = np.random.default_rng(11)
        X = rng.normal(size=(80, 1))
        X = X - X.mean(axis=0, keepdims=True)
        y = 50.0 + 3.0 * X[:, 0]
        r0 = fit_huber(
            X, y, delta=1e6, reg_lambda=0.0, max_iter=200, tol=1e-11
        )
        r1 = fit_huber(
            X, y, delta=1e6, reg_lambda=100.0, max_iter=200, tol=1e-11
        )
        self.assertLess(
            abs(r1["coefficients"][0]), abs(r0["coefficients"][0])
        )
        self.assertAlmostEqual(r1["intercept"], 50.0, places=8)
        self.assertAlmostEqual(r0["intercept"], 50.0, places=8)
        # 直接验证：惩罚只作用于系数列，截距列正规方程里无 λ 项
        from robust_regression.linalg import solve_weighted_ridge

        w = np.ones(80)
        sol = solve_weighted_ridge(X, y, w, 1e8, fit_intercept=True)
        self.assertAlmostEqual(sol["beta"][-1], 50.0, places=8)
        # 80 样本方差≈1，λ=1e8 把斜率压到 3·80/1e8 ≈ 2.4e-6
        self.assertAlmostEqual(sol["beta"][0], 0.0, places=5)

    def test_auto_delta_on_contaminated_high_leverage_is_stable(self):
        # 高杠杆单侧污染会让 OLS-MAD 偏大（文档化行为）：
        # auto δ 不保证小偏差，但结果必须收敛且不劣于 OLS 初值
        X, y, out_idx = outlier_data()
        res = fit_huber(X, y, delta="auto", max_iter=300, tol=1e-9)
        self.assertEqual(res["status"], "converged")
        self.assertGreater(res["delta_used"], 0.0)
        self.assertLessEqual(
            res["objective"], res["objective_history"][0] + 1e-9
        )

    def test_single_observation(self):
        # 单样本、秩亏：最小范数解把解释力放到截距列（列范数相同，
        # 解析解为 coef=7·2/(4+1)=2.8, intercept=1.4），残差为零
        X = np.array([[2.0]])
        y = np.array([7.0])
        res = fit_huber(X, y, delta="auto", max_iter=50)
        self.assertEqual(res["status"], "converged")
        self.assertTrue(res["rank_deficient"])
        self.assertAlmostEqual(res["objective"], 0.0, places=12)
        self.assertAlmostEqual(res["intercept"], 1.4, places=9)
        self.assertAlmostEqual(res["coefficients"][0], 2.8, places=9)


if __name__ == "__main__":
    unittest.main()
