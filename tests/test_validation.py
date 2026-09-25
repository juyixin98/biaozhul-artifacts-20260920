"""输入校验测试：范围、维度、有限性、失败状态。"""

import numpy as np
import unittest

from robust_regression import fit_huber, fit_ols
from robust_regression.validation import InputError, NumericalError


def good_xy(n=20, p=2, seed=0):
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, p))
    y = X @ np.ones(p) + rng.normal(scale=0.1, size=n)
    return X, y


class TestInputValidation(unittest.TestCase):
    def test_bad_shapes(self):
        X, y = good_xy()
        with self.assertRaises(InputError):
            fit_huber([[1, 2], [3, 4]], [1.0])  # 长度不一致
        with self.assertRaises(InputError):
            fit_huber([1, 2, 3], [1, 2, 3])  # X 一维
        with self.assertRaises(InputError):
            fit_huber(X, y.reshape(-1, 1))  # y 二维
        with self.assertRaises(InputError):
            fit_huber(np.zeros((0, 2)), np.zeros(0))
        with self.assertRaises(InputError):
            fit_huber(np.zeros((5, 0)), np.zeros(5))

    def test_non_finite_values(self):
        X, y = good_xy()
        Xb = X.copy()
        Xb[0, 0] = np.nan
        with self.assertRaises(InputError):
            fit_huber(Xb, y)
        yb = y.copy()
        yb[1] = np.inf
        with self.assertRaises(InputError):
            fit_huber(X, yb)

    def test_magnitude_limit(self):
        X, y = good_xy()
        Xb = X.copy()
        Xb[0, 0] = 2e8
        with self.assertRaises(InputError):
            fit_huber(Xb, y)

    def test_size_limit(self):
        X = np.zeros((100_001, 1))
        y = np.zeros(100_001)
        with self.assertRaises(InputError):
            fit_huber(X, y)
        X2 = np.zeros((5, 201))
        y2 = np.zeros(5)
        with self.assertRaises(InputError):
            fit_huber(X2, y2)

    def test_bad_delta(self):
        X, y = good_xy()
        for bad in [0.0, -1.0, 1e13, float("nan")]:
            with self.assertRaises(InputError):
                fit_huber(X, y, delta=bad)
        with self.assertRaises(InputError):
            fit_huber(X, y, delta="mad")

    def test_bad_reg_lambda(self):
        X, y = good_xy()
        for bad in [-0.1, 1e13, float("inf")]:
            with self.assertRaises(InputError):
                fit_huber(X, y, reg_lambda=bad)

    def test_bad_tol(self):
        X, y = good_xy()
        for bad in [0.0, -1e-8, 2.0]:
            with self.assertRaises(InputError):
                fit_huber(X, y, tol=bad)

    def test_bad_max_iter(self):
        X, y = good_xy()
        for bad in [0, -1, 10_001, 1.5, True]:
            with self.assertRaises(InputError):
                fit_huber(X, y, max_iter=bad)

    def test_bad_fit_intercept(self):
        X, y = good_xy()
        with self.assertRaises(InputError):
            fit_huber(X, y, fit_intercept=1)

    def test_boolean_not_treated_as_int(self):
        X, y = good_xy()
        with self.assertRaises(InputError):
            fit_huber(X, y, delta=True)

    def test_non_numeric_strings_rejected(self):
        X = [["a", 1], [2, 3]]
        y = [1.0, 2.0]
        with self.assertRaises(InputError):
            fit_huber(X, y)
        with self.assertRaises(InputError):
            fit_huber([[1.0, 2.0], [3.0, 4.0]], ["x", 2.0])

    def test_ragged_rows_rejected(self):
        with self.assertRaises(InputError):
            fit_huber([[1.0, 2.0], [3.0]], [1.0, 2.0])

    def test_ols_validates_too(self):
        with self.assertRaises(InputError):
            fit_ols([[1.0, 2.0]], [1.0, 2.0])

    def test_numerical_error_class_exists(self):
        # 文档化的数值失败状态类型必须可用
        self.assertTrue(issubclass(NumericalError, RuntimeError))


if __name__ == "__main__":
    unittest.main()
