"""linalg 层测试：SVD 最小二乘、秩亏处理、加权岭回归正规方程。"""

import numpy as np
import unittest

from robust_regression.linalg import (
    solve_ols,
    solve_weighted_ridge,
    svd_min_norm,
)


class TestOLS(unittest.TestCase):
    def test_exact_fit(self):
        X = np.arange(10, dtype=np.float64).reshape(10, 1)
        y = 3.0 * X[:, 0] - 2.0
        sol = solve_ols(X, y, fit_intercept=True)
        self.assertAlmostEqual(sol["coef"][0], 3.0, places=10)
        self.assertAlmostEqual(sol["intercept"], -2.0, places=10)
        self.assertEqual(sol["rank"], 2)
        self.assertFalse(sol["rank_deficient"])

    def test_matches_numpy_lstsq_full_rank(self):
        rng = np.random.default_rng(42)
        X = rng.normal(size=(50, 3))
        true_b = np.array([1.5, -2.0, 0.7])
        y = X @ true_b + 0.3 + rng.normal(scale=0.01, size=50)
        sol = solve_ols(X, y, fit_intercept=True)
        beta_np, *_ = np.linalg.lstsq(
            np.hstack([X, np.ones((50, 1))]), y, rcond=None
        )
        np.testing.assert_allclose(sol["beta"], beta_np, atol=1e-10)

    def test_collinear_duplicate_columns_min_norm(self):
        x = np.array([0.0, 1.0, 2.0, 3.0])
        X = np.column_stack([x, x, 2.0 * x])  # 秩 1
        y = 1.0 + 2.0 * x
        sol = solve_ols(X, y, fit_intercept=True)
        # 增广列 [x, x, 2x, 1] 秩为 2
        self.assertEqual(sol["rank"], 2)
        self.assertTrue(sol["rank_deficient"])
        # 最小范数解：斜率按列向量长度反比分配，预测精确
        np.testing.assert_allclose(X @ sol["coef"] + sol["intercept"], y)
        beta_np, *_ = np.linalg.lstsq(
            np.hstack([X, np.ones((4, 1))]), y, rcond=None
        )
        np.testing.assert_allclose(sol["beta"], beta_np, atol=1e-10)

    def test_zero_column_rank_deficient(self):
        X = np.zeros((6, 1))
        y = np.array([5.0] * 6)
        sol = solve_ols(X, y, fit_intercept=True)
        self.assertTrue(sol["rank_deficient"])
        self.assertAlmostEqual(sol["coef"][0], 0.0)
        self.assertAlmostEqual(sol["intercept"], 5.0)

    def test_no_intercept(self):
        X = np.array([[1.0], [2.0], [3.0]])
        y = 2.0 * X[:, 0]
        sol = solve_ols(X, y, fit_intercept=False)
        self.assertAlmostEqual(sol["coef"][0], 2.0)
        self.assertEqual(sol["intercept"], 0.0)
        self.assertFalse(sol["rank_deficient"])


class TestWeightedRidge(unittest.TestCase):
    def _normal_equation_residual(self, X, y, w, lam, fit_intercept):
        n, p = X.shape
        sol = solve_weighted_ridge(X, y, w, lam, fit_intercept)
        sw = np.sqrt(w)
        if fit_intercept:
            A = np.hstack([X, np.ones((n, 1))]) * sw[:, None]
            pen = np.concatenate(
                [np.full(p, lam), np.zeros(1)]
            )
        else:
            A = X * sw[:, None]
            pen = np.full(p, lam)
        yw = y * sw
        residual = (A.T @ A + np.diag(pen)) @ sol["beta"] - A.T @ yw
        return residual, sol

    def test_normal_equations_unregularized(self):
        rng = np.random.default_rng(0)
        X = rng.normal(size=(30, 3))
        y = rng.normal(size=30)
        w = rng.uniform(0.1, 2.0, size=30)
        residual, sol = self._normal_equation_residual(
            X, y, w, 0.0, True
        )
        self.assertLess(np.max(np.abs(residual)), 1e-8)
        self.assertFalse(sol["rank_deficient"])

    def test_normal_equations_ridge_intercept_unpenalized(self):
        rng = np.random.default_rng(1)
        X = rng.normal(size=(40, 2))
        y = 100.0 + X[:, 0] - X[:, 1] + rng.normal(scale=0.1, size=40)
        w = np.full(40, 1.0)
        residual, sol = self._normal_equation_residual(
            X, y, w, 10.0, True
        )
        self.assertLess(np.max(np.abs(residual)), 1e-7)
        # 大截距不被岭惩罚压小
        self.assertAlmostEqual(sol["beta"][-1], 100.0, delta=1.0)

    def test_rank_diagnosis_with_penalty_still_flags_collinearity(self):
        x = np.array([1.0, 2.0, 3.0, 4.0])
        X = np.column_stack([x, x])
        y = np.array([1.0, 3.0, 5.0, 7.0])
        w = np.ones(4)
        sol = solve_weighted_ridge(X, y, w, 1.0, True)
        self.assertTrue(sol["rank_deficient"])
        self.assertEqual(sol["rank"], 2)  # [x, x, 1] -> 秩 2

    def test_unpenalized_rank_deficient_min_norm(self):
        x = np.array([1.0, 2.0, 3.0])
        X = np.column_stack([x, x])
        y = 2.0 * x
        w = np.ones(3)
        sol = solve_weighted_ridge(X, y, w, 0.0, False)
        self.assertTrue(sol["rank_deficient"])
        np.testing.assert_allclose(X @ sol["beta"], y, atol=1e-10)


class TestSVDMinNorm(unittest.TestCase):
    def test_tall_full_rank(self):
        A = np.array([[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]])
        b = np.array([1.0, 2.0, 3.0])
        beta, rank, deficient = svd_min_norm(A, b)
        self.assertEqual(rank, 2)
        self.assertFalse(deficient)
        np.testing.assert_allclose(beta, [1.0, 2.0], atol=1e-10)

    def test_all_zero_matrix(self):
        A = np.zeros((4, 2))
        b = np.array([1.0, 2.0, 3.0, 4.0])
        beta, rank, deficient = svd_min_norm(A, b)
        self.assertEqual(rank, 0)
        self.assertTrue(deficient)
        np.testing.assert_allclose(beta, [0.0, 0.0])


if __name__ == "__main__":
    unittest.main()
