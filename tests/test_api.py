"""JSON 接口测试：字段校验、成功/失败响应形状、CLI 端到端。"""

import json
import subprocess
import sys
import unittest

from adaptive_integration import handle_request


def _req(**overrides):
    req = {"expression": "exp(-x**2)", "a": 0.0, "b": 1.0}
    req.update(overrides)
    return req


class TestSuccessShape(unittest.TestCase):
    def test_defaults(self):
        r = handle_request(_req())
        self.assertEqual(r["status"], "converged")
        self.assertTrue(r["converged"])
        self.assertAlmostEqual(r["value"], 0.7468241328, places=7)
        self.assertLessEqual(r["error_estimate"], 2e-8)
        self.assertGreater(r["n_evals"], 0)
        self.assertEqual(r["error_code"], None)
        self.assertEqual(r["request_echo"]["method"], "simpson")

    def test_gauss_method(self):
        r = handle_request(_req(method="gauss", eps_abs=1e-12, eps_rel=1e-12))
        self.assertEqual(r["status"], "converged")
        self.assertAlmostEqual(r["value"], 0.7468241328, places=10)


class TestRequestValidation(unittest.TestCase):
    def assert_invalid(self, req, code="INVALID_REQUEST"):
        r = handle_request(req)
        self.assertEqual(r["status"], "invalid_request")
        self.assertEqual(r["error_code"], code)
        self.assertIsNone(r["value"])

    def test_not_object(self):
        self.assert_invalid([1, 2, 3])

    def test_missing_fields(self):
        r = handle_request({"a": 0})
        self.assertEqual(r["status"], "invalid_request")
        self.assertIn("expression", r["details"]["missing_fields"])
        self.assertIn("b", r["details"]["missing_fields"])

    def test_unknown_field(self):
        self.assert_invalid(_req(callback="x"))

    def test_non_finite_bounds(self):
        self.assert_invalid(_req(a="0"))
        self.assert_invalid(_req(b=True))
        import math
        self.assert_invalid(_req(b=math.inf))

    def test_bad_tolerances(self):
        self.assert_invalid(_req(eps_abs=-1))
        self.assert_invalid(_req(eps_abs=0, eps_rel=0))
        self.assert_invalid(_req(eps_rel=2))

    def test_bad_method(self):
        self.assert_invalid(_req(method="trapezoid"))

    def test_bad_limits(self):
        self.assert_invalid(_req(max_depth=0))
        self.assert_invalid(_req(max_depth=41))
        self.assert_invalid(_req(max_evals=2_000_000))

    def test_parse_error_caret(self):
        r = handle_request(_req(expression="x^2"))
        self.assertEqual(r["error_code"], "PARSE_ERROR")

    def test_injection_rejected(self):
        r = handle_request(_req(expression="__import__('os')"))
        self.assertEqual(r["error_code"], "PARSE_ERROR")


class TestFailedIntegrationsViaApi(unittest.TestCase):
    def test_endpoint_singularity(self):
        r = handle_request(_req(expression="1/sqrt(x)", a=0.0, b=1.0))
        self.assertEqual(r["status"], "failed")
        self.assertFalse(r["converged"])
        self.assertEqual(r["error_code"], "SINGULAR_ENDPOINT")
        self.assertIsNone(r["value"])
        self.assertEqual(r["details"]["position"], "endpoint")

    def test_narrow_peak_depth(self):
        r = handle_request(_req(
            expression="1e-8/(pi*(x**2+1e-16))", a=0.0, b=1.0,
            max_depth=15, max_evals=1_000_000))
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["error_code"], "MAX_DEPTH_REACHED")

    def test_aliasing_reports_converged_but_wrong(self):
        # 文档化的“误差估计失效”请求：库诚实地报告 converged，
        # 验收研究里会把真实误差展示出来。
        r = handle_request(_req(
            expression="sin(16*pi*x+0.3)", a=0.0, b=1.0,
            eps_abs=1e-10, eps_rel=1e-10))
        self.assertEqual(r["status"], "converged")
        self.assertLess(r["error_estimate"], 1e-12)
        self.assertGreater(abs(r["value"]), 1e-2)


class TestCli(unittest.TestCase):
    def _run_cli(self, payload: str) -> tuple[int, dict]:
        proc = subprocess.run(
            [sys.executable, "-m", "adaptive_integration.cli"],
            input=payload, capture_output=True, text=True, check=False)
        return proc.returncode, json.loads(proc.stdout)

    def test_valid_stdin(self):
        code, r = self._run_cli(json.dumps(_req()))
        self.assertEqual(code, 0)
        self.assertEqual(r["status"], "converged")

    def test_malformed_json_exit_2(self):
        code, r = self._run_cli("{not json")
        self.assertEqual(code, 2)
        self.assertEqual(r["error_code"], "MALFORMED_JSON")

    def test_invalid_request_exit_0(self):
        code, r = self._run_cli(json.dumps({"a": 0}))
        self.assertEqual(code, 0)
        self.assertEqual(r["status"], "invalid_request")

    def test_failed_integration_exit_0(self):
        code, r = self._run_cli(json.dumps(
            _req(expression="1/sqrt(x)", a=0.0, b=1.0)))
        self.assertEqual(code, 0)
        self.assertEqual(r["error_code"], "SINGULAR_ENDPOINT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
