"""补齐输入校验与合成数据参数校验的分支覆盖。"""

from __future__ import annotations

import unittest

from calibration.errors import CalibrationError
from calibration.metrics import brier_score
from calibration.synthetic import make_demo_dataset, make_synthetic_labels


class ValidationBranchTest(unittest.TestCase):
    def test_non_numeric_probability_rejected(self):
        with self.assertRaises(CalibrationError) as ctx:
            brier_score([0, 1], [0.5, "oops"])  # type: ignore[list-item]
        self.assertEqual(ctx.exception.code, "INVALID_INPUT")

    def test_nan_label_rejected(self):
        # NaN 属于非有限值，先被有限性检查拦下。
        with self.assertRaises(CalibrationError) as ctx:
            brier_score([0.0, float("nan")], [0.5, 0.5])
        self.assertEqual(ctx.exception.code, "INVALID_INPUT")

    def test_inf_label_rejected(self):
        with self.assertRaises(CalibrationError):
            brier_score([0.0, float("inf")], [0.5, 0.5])

    def test_nan_weight_rejected(self):
        with self.assertRaises(CalibrationError):
            brier_score([0, 1], [0.5, 0.5], sample_weight=[1.0, float("nan")])

    def test_non_one_dimensional_rejected(self):
        with self.assertRaises(CalibrationError) as ctx:
            brier_score([[0, 1]], [[0.5, 0.5]])
        self.assertEqual(ctx.exception.code, "INVALID_INPUT")


class SyntheticParamTest(unittest.TestCase):
    def test_bad_n_samples_rejected(self):
        for bad in (0, -1, 1.5, "100"):
            with self.subTest(bad=bad):
                with self.assertRaises(CalibrationError):
                    make_synthetic_labels(bad)  # type: ignore[arg-type]

    def test_bad_prior_rejected(self):
        for bad in (0.0, 1.0, -0.1, 1.5):
            with self.subTest(bad=bad):
                with self.assertRaises(CalibrationError):
                    make_synthetic_labels(10, prior=bad)

    def test_demo_dataset_keys(self):
        data = make_demo_dataset(20, seed=1)
        for key in ("X", "y", "p_true", "proba", "positive_prevalence"):
            self.assertIn(key, data)


if __name__ == "__main__":
    unittest.main()
