"""JSON API 层契约测试。"""

import copy
import json
import unittest

import numpy as np

from robust_regression import fit_from_json

def _canonical_request():
    rng = np.random.default_rng(1)
    n = 30
    x = np.linspace(0.0, 10.0, n)
    x = x - x.mean()
    y = 2.0 * x + 1.0 + rng.normal(scale=0.3, size=n)
    idx = np.linspace(4, n - 5, 4).astype(int)
    y[idx] += 8.0 * np.array([1.0, -1.0, 1.0, -1.0])
    return {
        "method": "huber",
        "X": x.reshape(-1, 1).tolist(),
        "y": y.tolist(),
        "delta": 0.5,
        "tol": 1e-10,
        "max_iter": 300,
        "fit_intercept": True,
    }


SAMPLE_REQUEST = _canonical_request()


def _json_roundtrip(obj):
    return json.loads(json.dumps(obj, allow_nan=False))


class TestAPIContract(unittest.TestCase):
    def test_success_shape_and_json_serializable(self):
        resp = fit_from_json(SAMPLE_REQUEST)
        # 不得包含 NaN/Infinity（allow_nan=False 会抛错）
        _json_roundtrip(resp)
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["method"], "huber")
        r = resp["result"]
        self.assertEqual(r["status"], "converged")
        for key in (
            "coefficients",
            "intercept",
            "delta_used",
            "iterations",
            "converged",
            "objective",
            "objective_history",
            "rank",
            "rank_deficient",
            "warnings",
        ):
            self.assertIn(key, r)
        self.assertIsInstance(r["coefficients"], list)
        self.assertIsInstance(r["objective_history"], list)

    def test_robustness_through_json(self):
        resp = fit_from_json(SAMPLE_REQUEST)
        r = resp["result"]
        ols = fit_from_json(
            {"method": "ols", "X": SAMPLE_REQUEST["X"], "y": SAMPLE_REQUEST["y"]}
        )["result"]
        coef = r["coefficients"][0]
        intercept = r["intercept"]
        # 4 个 ±8 中部垂直离群点：Huber 精确恢复（误差 <0.05），
        # OLS 斜率被拉偏（约 1.83）。
        self.assertAlmostEqual(coef, 2.0, delta=0.05)
        self.assertAlmostEqual(intercept, 1.0, delta=0.05)
        self.assertLess(abs(coef - 2.0), abs(ols["coefficients"][0] - 2.0))

    def test_ols_method(self):
        payload = {
            "method": "ols",
            "X": SAMPLE_REQUEST["X"],
            "y": SAMPLE_REQUEST["y"],
        }
        resp = fit_from_json(payload)
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["method"], "ols")
        self.assertIn("objective_sse", resp["result"])
        # OLS 受离群点拉扯，斜率偏离 2
        self.assertNotAlmostEqual(
            resp["result"]["coefficients"][0], 2.0, delta=0.1
        )

    def test_defaults(self):
        resp = fit_from_json(
            {"X": SAMPLE_REQUEST["X"], "y": SAMPLE_REQUEST["y"]}
        )
        self.assertTrue(resp["ok"])
        r = resp["result"]
        self.assertEqual(r["tol"], 1e-7)
        self.assertEqual(r["max_iter"], 100)
        self.assertTrue(r["fit_intercept"])
        self.assertEqual(r["reg_lambda"], 0.0)

    def test_missing_fields(self):
        resp = fit_from_json({"X": [[1.0]]})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["type"], "input_error")

    def test_unknown_method(self):
        bad = copy.deepcopy(SAMPLE_REQUEST)
        bad["method"] = "lasso"
        resp = fit_from_json(bad)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["type"], "input_error")

    def test_bad_delta_string(self):
        bad = copy.deepcopy(SAMPLE_REQUEST)
        bad["delta"] = "median"
        resp = fit_from_json(bad)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["type"], "input_error")

    def test_size_mismatch(self):
        bad = copy.deepcopy(SAMPLE_REQUEST)
        bad["y"] = [1.0, 2.0]
        resp = fit_from_json(bad)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["type"], "input_error")

    def test_non_finite(self):
        bad = copy.deepcopy(SAMPLE_REQUEST)
        bad["X"][0][0] = "nan"
        resp = fit_from_json(bad)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["type"], "input_error")

    def test_payload_not_object(self):
        resp = fit_from_json([1, 2, 3])
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["type"], "input_error")

    def test_collinear_request(self):
        x = [0.0, 1.0, 2.0, 3.0, 4.0]
        payload = {
            "X": [[v, v, 2.0 * v] for v in x],
            "y": [4.0 * v + 0.5 for v in x],
            "delta": 1.0,
        }
        resp = fit_from_json(payload)
        self.assertTrue(resp["ok"])
        r = resp["result"]
        self.assertTrue(r["rank_deficient"])
        self.assertIn("rank_deficient_min_norm_solution", r["warnings"])
        self.assertEqual(r["status"], "converged")

    def test_zero_residual_request(self):
        payload = {
            "X": [[1.0], [2.0], [3.0]],
            "y": [3.0, 6.0, 9.0],
            "fit_intercept": False,
        }
        resp = fit_from_json(payload)
        self.assertTrue(resp["ok"])
        r = resp["result"]
        self.assertAlmostEqual(r["objective"], 0.0, places=12)
        self.assertIn(
            "auto_scale_all_zero_residuals", r["warnings"]
        )

    def test_max_iter_reached_through_api(self):
        payload = {
            **SAMPLE_REQUEST,
            "max_iter": 2,
            "tol": 1e-13,
        }
        resp = fit_from_json(payload)
        self.assertTrue(resp["ok"])
        self.assertEqual(
            resp["result"]["status"], "max_iterations_reached"
        )

    def test_no_nan_infinity_tokens_even_on_failure(self):
        resp = fit_from_json({"X": [[float("inf")]], "y": [1.0]})
        # allow_nan=False 保证输出不含 JSON 非法 token NaN/Infinity
        text = json.dumps(resp, allow_nan=False)
        self.assertNotIn("Infinity", text)
        self.assertNotIn(": NaN", text)
        self.assertFalse(resp["ok"])


if __name__ == "__main__":
    unittest.main()
