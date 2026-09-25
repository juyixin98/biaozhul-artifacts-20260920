"""CLI 端到端测试：标准输入 JSON -> 标准输出 JSON、退出码、可复现。"""

import json
import subprocess
import sys
import unittest

import numpy as np


def _canonical_request():
    rng = np.random.default_rng(1)
    n = 30
    x = np.linspace(0.0, 10.0, n)
    x = x - x.mean()
    y = 2.0 * x + 1.0 + rng.normal(scale=0.3, size=n)
    idx = np.linspace(4, n - 5, 4).astype(int)
    y[idx] += 8.0 * np.array([1.0, -1.0, 1.0, -1.0])
    return {
        "X": x.reshape(-1, 1).tolist(),
        "y": y.tolist(),
        "delta": 0.5,
        "tol": 1e-10,
        "max_iter": 300,
    }


REQUEST = _canonical_request()


def run_cli(payload_text):
    proc = subprocess.run(
        [sys.executable, "-m", "robust_regression.cli"],
        input=payload_text,
        capture_output=True,
        text=True,
    )
    return proc


class TestCLI(unittest.TestCase):
    def test_success_exit_code_zero(self):
        proc = run_cli(json.dumps(REQUEST))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        resp = json.loads(proc.stdout)
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["status"], "converged")

    def test_input_error_exit_code_one(self):
        bad = dict(REQUEST)
        bad["y"] = [1.0]
        proc = run_cli(json.dumps(bad))
        self.assertEqual(proc.returncode, 1)
        resp = json.loads(proc.stdout)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["type"], "input_error")

    def test_malformed_json_exit_code_two(self):
        proc = run_cli("{not valid json")
        self.assertEqual(proc.returncode, 2)
        resp = json.loads(proc.stdout)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["type"], "input_error")

    def test_empty_input_exit_code_two(self):
        proc = run_cli("")
        self.assertEqual(proc.returncode, 2)

    def test_two_processes_identical_output(self):
        text = json.dumps(REQUEST)
        p1 = run_cli(text)
        p2 = run_cli(text)
        self.assertEqual(p1.stdout, p2.stdout)

    def test_ols_request_via_cli(self):
        proc = run_cli(
            json.dumps(
                {"method": "ols", "X": REQUEST["X"], "y": REQUEST["y"]}
            )
        )
        self.assertEqual(proc.returncode, 0)
        resp = json.loads(proc.stdout)
        self.assertEqual(resp["method"], "ols")
        self.assertIn("objective_sse", resp["result"])


if __name__ == "__main__":
    unittest.main()
