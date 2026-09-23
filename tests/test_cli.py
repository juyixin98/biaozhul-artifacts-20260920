"""End-to-end CLI tests: real subprocess, JSON stdin/stdout, exit codes."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

import numpy as np

from sparse_cg.generators import laplacian_1d, make_rhs_from_exact_solution, smooth_x

REPO = Path(__file__).resolve().parents[1]


def run_cli(payload, *, input_path=None):
    """Invoke the CLI; returns (exit_code, parsed_json)."""
    stdin = None if input_path else json.dumps(payload)
    cmd = [sys.executable, "-m", "sparse_cg.cli"]
    if input_path:
        cmd += ["-i", str(input_path)]
    proc = subprocess.run(
        cmd, input=stdin, capture_output=True, text=True, cwd=REPO,
    )
    parsed = None
    if proc.stdout.strip():
        parsed = json.loads(proc.stdout)
    return proc.returncode, parsed, proc.stderr


class TestCliEndToEnd(unittest.TestCase):

    def test_valid_request_exit_0_and_converged(self):
        n = 25
        A, _ = laplacian_1d(n)
        b, _ = make_rhs_from_exact_solution(A, smooth_x(n))
        payload = {
            "matrix": {"n": n, "indptr": A.indptr.tolist(),
                       "indices": A.indices.tolist(),
                       "data": A.data.tolist()},
            "b": b.tolist(), "tol": 1e-10,
        }
        code, resp, err = run_cli(payload)
        self.assertEqual(code, 0, err)
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["status"], "converged")
        self.assertLess(resp["result"]["relative_residual"], 1e-10)

    def test_invalid_csr_exit_2(self):
        payload = {
            "matrix": {"n": 2, "indptr": [0, 5, 5],
                       "indices": [0, 1], "data": [1.0, 2.0]},
            "b": [1.0, 1.0],
        }
        code, resp, err = run_cli(payload)
        self.assertEqual(code, 2)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "invalid_csr")

    def test_non_symmetric_exit_2(self):
        payload = {
            "matrix": {"n": 2, "indptr": [0, 2, 3],
                       "indices": [0, 1, 1], "data": [2.0, -1.0, 2.0]},
            "b": [1.0, 1.0],
        }
        code, resp, _ = run_cli(payload)
        self.assertEqual(code, 2)
        self.assertEqual(resp["error"]["code"], "non_symmetric_matrix")

    def test_malformed_json_exit_2(self):
        proc = subprocess.run(
            [sys.executable, "-m", "sparse_cg.cli"],
            input="{not valid json", capture_output=True, text=True, cwd=REPO,
        )
        self.assertEqual(proc.returncode, 2)
        resp = json.loads(proc.stdout)
        self.assertEqual(resp["error"]["code"], "invalid_json")

    def test_non_positive_curvature_exit_0_but_not_converged(self):
        # A failed *solve* (not a rejected request) exits 0; convergence is
        # read from the response body.
        payload = {
            "matrix": {"n": 2, "indptr": [0, 2, 4],
                       "indices": [0, 1, 0, 1],
                       "data": [2.0, 3.0, 3.0, 2.0]},
            "b": [1.0, -1.0], "assume_spd": True,
        }
        code, resp, _ = run_cli(payload)
        self.assertEqual(code, 0)
        self.assertTrue(resp["ok"])
        self.assertFalse(resp["result"]["converged"])
        self.assertEqual(resp["result"]["status"],
                         "non_positive_curvature")

    def test_example_files_round_trip(self):
        # Every shipped request example runs through the CLI; valid ones
        # converge (or zero_rhs), invalid ones exit 2 with a stable code.
        examples = REPO / "examples"
        expected = {
            "request_basic.json": (0, {"converged", "zero_rhs"}),
            "request_zero_rhs.json": (0, {"zero_rhs"}),
            "request_ill_conditioned.json": (0, {"converged"}),
            "request_invalid_csr.json": (2, None),
            "request_non_symmetric.json": (2, None),
            "request_coo_grid.json": (0, {"converged"}),
        }
        for name, (exp_code, exp_statuses) in expected.items():
            path = examples / name
            self.assertTrue(path.exists(), f"missing shipped example {name}")
            code, resp, err = run_cli(None, input_path=path)
            self.assertEqual(code, exp_code, f"{name}: {err or resp}")
            if exp_statuses is not None:
                self.assertIn(resp["result"]["status"], exp_statuses, name)
            else:
                self.assertFalse(resp["ok"], name)

    def test_output_file_option(self):
        n = 10
        A, _ = laplacian_1d(n)
        b, _ = make_rhs_from_exact_solution(A, smooth_x(n))
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "resp.json"
            proc = subprocess.run(
                [sys.executable, "-m", "sparse_cg.cli",
                 "-o", str(out), "--pretty"],
                input=json.dumps({
                    "matrix": {"n": n, "indptr": A.indptr.tolist(),
                               "indices": A.indices.tolist(),
                               "data": A.data.tolist()},
                    "b": b.tolist(),
                }),
                capture_output=True, text=True, cwd=REPO,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(proc.stdout.strip())
            saved = json.loads(out.read_text())
            self.assertTrue(saved["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
