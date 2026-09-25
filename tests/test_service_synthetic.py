"""服务层与合成数据测试。"""

from __future__ import annotations

import unittest

import numpy as np

from calibration.metrics import brier_score, log_loss
from calibration.service import CalibrationService
from calibration.synthetic import (
    logistic_sigmoid,
    make_demo_dataset,
    make_synthetic_labels,
    simple_model_proba,
)


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.service = CalibrationService()

    def test_success_envelope(self):
        resp = self.service.evaluate(
            {"y_true": [0, 1, 1, 0], "proba": [0.1, 0.9, 0.8, 0.4]}
        )
        self.assertTrue(resp["success"])
        self.assertIsNone(resp["error"])
        data = resp["data"]
        for key in ("brier_score", "log_loss", "ece", "bins", "n_samples"):
            self.assertIn(key, data)
        self.assertEqual(data["n_samples"], 4)
        self.assertEqual(data["n_bins"], 10)
        self.assertEqual(len(data["bins"]), 10)

    def test_invalid_probability_envelope(self):
        resp = self.service.evaluate(
            {"y_true": [0, 1], "proba": [0.5, 1.5]}
        )
        self.assertFalse(resp["success"])
        self.assertIsNone(resp["data"])
        self.assertEqual(resp["error"]["code"], "INVALID_PROBABILITY")

    def test_missing_field_envelope(self):
        resp = self.service.evaluate({"y_true": [0, 1]})
        self.assertFalse(resp["success"])
        self.assertEqual(resp["error"]["code"], "MISSING_FIELD")

    def test_request_not_object(self):
        resp = self.service.evaluate([1, 2, 3])  # type: ignore[arg-type]
        self.assertFalse(resp["success"])
        self.assertEqual(resp["error"]["code"], "INVALID_REQUEST")

    def test_options_forwarded(self):
        request = {
            "y_true": [1, 0],
            "proba": [0.0, 1.0],
            "n_bins": 4,
            "endpoint_strategy": "clip",
            "epsilon": 1e-8,
        }
        resp = self.service.evaluate(request)
        self.assertTrue(resp["success"])
        self.assertEqual(resp["data"]["n_bins"], 4)
        self.assertAlmostEqual(
            resp["data"]["log_loss"], -np.log(1e-8), places=6
        )

    def test_bad_strategy_via_service(self):
        resp = self.service.evaluate(
            {
                "y_true": [0, 1],
                "proba": [0.2, 0.8],
                "endpoint_strategy": "banana",
            }
        )
        self.assertFalse(resp["success"])
        self.assertEqual(resp["error"]["code"], "INVALID_STRATEGY")


class SyntheticDataTest(unittest.TestCase):
    def test_reproducible_with_seed(self):
        a = make_synthetic_labels(200, seed=99)
        b = make_synthetic_labels(200, seed=99)
        np.testing.assert_array_equal(a["y"], b["y"])
        np.testing.assert_array_equal(a["X"], b["X"])
        np.testing.assert_array_equal(a["p_true"], b["p_true"])

    def test_different_seeds_differ(self):
        a = make_synthetic_labels(200, seed=1)
        b = make_synthetic_labels(200, seed=2)
        self.assertFalse(np.array_equal(a["X"], b["X"]))

    def test_probabilities_in_unit_interval(self):
        data = make_demo_dataset(500, seed=3)
        self.assertTrue(np.all(data["proba"] >= 0.0))
        self.assertTrue(np.all(data["proba"] <= 1.0))
        self.assertTrue(np.all(data["p_true"] > 0.0))
        self.assertTrue(np.all(data["p_true"] < 1.0))

    def test_prior_drives_prevalence(self):
        rare = make_synthetic_labels(4000, seed=5, prior=0.05)
        common = make_synthetic_labels(4000, seed=5, prior=0.5)
        self.assertLess(np.mean(rare["y"]), np.mean(common["y"]))

    def test_true_probabilities_are_better_calibrated_than_model(self):
        data = make_demo_dataset(2000, seed=42, temperature=2.0)
        y = data["y"]
        ll_true = log_loss(y, data["p_true"])
        ll_model = log_loss(y, data["proba"])
        bs_true = brier_score(y, data["p_true"])
        bs_model = brier_score(y, data["proba"])
        # 数据生成所用的真实模型，期望上比温度失配模型校准得更好。
        self.assertLess(ll_true, ll_model)
        self.assertLess(bs_true, bs_model)

    def test_sigmoid_matches_brute_force(self):
        x = np.array([-100.0, -1.0, 0.0, 1.0, 100.0])
        expected = 1.0 / (1.0 + np.exp(-np.clip(x, -50, 50)))
        np.testing.assert_allclose(logistic_sigmoid(x), expected, atol=1e-12)

    def test_invalid_temperature_rejected(self):
        with self.assertRaises(ValueError):
            simple_model_proba(np.zeros(3), temperature=0.0)


if __name__ == "__main__":
    unittest.main()
