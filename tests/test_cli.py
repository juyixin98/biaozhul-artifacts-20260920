"""CLI 端到端测试。

主路径进程内调用 ``cli.main``（可被覆盖率统计），另保留一个真实
子进程冒烟测试，验证 ``python -m calibration.cli`` 入口确实可用。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from calibration.cli import main

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = PROJECT_ROOT / "examples"


def run_main(*args, stdin: str | None = None):
    """进程内运行 CLI，返回 (returncode, stdout_text)。"""
    buf = io.StringIO()
    old_stdin = sys.stdin
    try:
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        with contextlib.redirect_stdout(buf):
            code = main(list(args))
    finally:
        sys.stdin = old_stdin
    return code, buf.getvalue()


def run_subprocess(*args, stdin: str | None = None):
    return subprocess.run(
        [sys.executable, "-m", "calibration.cli", *args],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )


class CliTest(unittest.TestCase):
    def test_evaluate_file_success(self):
        code, out = run_main("evaluate", str(EXAMPLES / "request_basic.json"))
        self.assertEqual(code, 0)
        resp = json.loads(out)
        self.assertTrue(resp["success"])
        self.assertIn("brier_score", resp["data"])

    def test_evaluate_stdin(self):
        request = json.dumps({"y_true": [0, 1], "proba": [0.2, 0.8]})
        code, out = run_main("evaluate", "-", stdin=request)
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["success"])

    def test_evaluate_invalid_probability_exit_code_1(self):
        code, out = run_main(
            "evaluate", str(EXAMPLES / "request_invalid_probability.json")
        )
        self.assertEqual(code, 1)
        resp = json.loads(out)
        self.assertFalse(resp["success"])
        self.assertEqual(resp["error"]["code"], "INVALID_PROBABILITY")

    def test_invalid_json_file_returns_envelope(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False
        ) as fh:
            fh.write("{ not json")
            path = fh.name
        try:
            code, out = run_main("evaluate", path)
        finally:
            os.unlink(path)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], "INVALID_JSON")

    def test_demo_is_reproducible(self):
        code_a, out_a = run_main("demo", "--n-samples", "300", "--seed", "42")
        code_b, out_b = run_main("demo", "--n-samples", "300", "--seed", "42")
        self.assertEqual(code_a, 0)
        self.assertEqual(code_b, 0)
        self.assertEqual(out_a, out_b)
        data = json.loads(out_a)["data"]
        self.assertEqual(data["demo_meta"]["n_samples"], 300)
        # 真实概率的 ECE 应当明显小于欠校准模型。
        self.assertLess(
            data["demo_meta"]["ece_of_true_probability"], data["ece"]
        )

    def test_demo_extreme_imbalance_runs(self):
        code, out = run_main(
            "demo", "--n-samples", "2000", "--prior", "0.005",
            "--temperature", "0.5",
        )
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["success"])

    def test_real_subprocess_entrypoint_smoke(self):
        """确证 ``python -m calibration.cli`` 在全新解释器中可用。"""
        proc = run_subprocess("evaluate", str(EXAMPLES / "request_basic.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["success"])


if __name__ == "__main__":
    unittest.main()
