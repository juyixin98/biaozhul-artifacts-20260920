"""CLI 端到端测试：stdin / 文件输入、退出码、JSON 常量拒绝。"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

LAPLACIAN_REQUEST = {
    "matrix": {
        "n": 4,
        "data": [2.0, -1.0, -1.0, 2.0, -1.0, -1.0, 2.0, -1.0, -1.0, 2.0],
        "indices": [0, 1, 0, 1, 2, 1, 2, 3, 2, 3],
        "indptr": [0, 2, 5, 8, 10],
    },
    "b": [1.0, 0.0, 0.0, 1.0],
}


def run_cli(stdin: str | None = None, file: str | Path | None = None):
    cmd = [sys.executable, "-m", "sparse_cg.cli"]
    if file is not None:
        cmd.append(str(file))
    return subprocess.run(
        cmd,
        input=stdin,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )


class TestCLI(unittest.TestCase):
    def test_stdin_success(self) -> None:
        proc = run_cli(stdin=json.dumps(LAPLACIAN_REQUEST))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        resp = json.loads(proc.stdout)
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["status"], "converged")
        self.assertEqual(len(resp["x"]), 4)
        # 真实残差达标
        import numpy as np

        A = np.array(
            [[2, -1, 0, 0], [-1, 2, -1, 0], [0, -1, 2, -1], [0, 0, -1, 2]],
            dtype=float,
        )
        b = np.array([1.0, 0.0, 0.0, 1.0])
        self.assertLess(np.linalg.norm(b - A @ np.array(resp["x"])), 1e-7)

    def test_file_input(self) -> None:
        path = REPO_ROOT / "examples" / "01_laplacian.json"
        if not path.exists():  # 脚本尚未生成时，用临时文件
            import tempfile, os

            fd, tmp = tempfile.mkstemp(suffix=".json")
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump(LAPLACIAN_REQUEST, fh)
                proc = run_cli(file=tmp)
            finally:
                os.unlink(tmp)
        else:
            proc = run_cli(file=path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["ok"])

    def test_invalid_csr_exit_zero_but_error_payload(self) -> None:
        # 非法 CSR 是合法 JSON 请求：退出码 0，ok=false（算法/请求结果走 JSON）
        bad = {
            "matrix": {
                "n": 2,
                "data": [1.0, 1.0],
                "indices": [5, 0],  # 越界
                "indptr": [0, 2, 2],
            },
            "b": [1.0, 1.0],
        }
        proc = run_cli(stdin=json.dumps(bad))
        self.assertEqual(proc.returncode, 0)
        resp = json.loads(proc.stdout)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "index_out_of_bounds")

    def test_malformed_json_exit_one(self) -> None:
        proc = run_cli(stdin='{"matrix": }')
        self.assertEqual(proc.returncode, 1)
        resp = json.loads(proc.stdout)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "invalid_json")

    def test_empty_input_exit_one(self) -> None:
        proc = run_cli(stdin="   \n")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "empty_request")

    def test_nan_constant_rejected(self) -> None:
        # Python json 能产出 NaN 常量，但不是合法 JSON
        proc = run_cli(
            stdin='{"matrix": {"n": 1, "data": [NaN], "indices": [0],'
            ' "indptr": [0, 1]}, "b": [1.0]}'
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "invalid_json")

    def test_missing_file_exit_one(self) -> None:
        proc = run_cli(file="does_not_exist.json")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(
            json.loads(proc.stdout)["error"]["code"], "file_unreadable"
        )

    def test_too_many_args_exit_two(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "sparse_cg.cli", "a.json", "b.json"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
