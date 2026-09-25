"""命令行接口端到端测试（子进程方式）。"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE = REPO_ROOT / "examples" / "request_basic.json"


class TestCli(unittest.TestCase):
    def _run(self, args=None, input_text=None):
        cmd = [sys.executable, "-m", "mospp"] + (args or [])
        proc = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            input=input_text,
            capture_output=True,
            text=True,
        )
        return proc

    def test_sample_file(self):
        proc = self._run([str(SAMPLE)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        r = json.loads(proc.stdout)
        self.assertEqual(r["status"], "ok")
        self.assertGreaterEqual(len(r["pareto_front"]), 2)

    def test_stdin_input(self):
        payload = json.loads(SAMPLE.read_text(encoding="utf-8"))
        proc = self._run(input_text=json.dumps(payload))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        r = json.loads(proc.stdout)
        self.assertEqual(r["status"], "ok")

    def test_compact_output(self):
        proc = self._run(["--compact", str(SAMPLE)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # 紧凑输出没有缩进换行
        self.assertNotIn("\n  ", proc.stdout)
        json.loads(proc.stdout)  # 仍然是合法 JSON

    def test_invalid_json_exit_code(self):
        proc = self._run(input_text="{not json")
        self.assertEqual(proc.returncode, 2)
        err = json.loads(proc.stderr)
        self.assertEqual(err["error"], "invalid_json")

    def test_validation_error_exit_code(self):
        proc = self._run(
            input_text=json.dumps(
                {"graph": {"nodes": [], "edges": []}}
            )
        )
        self.assertEqual(proc.returncode, 2)
        err = json.loads(proc.stderr)
        self.assertEqual(err["error"], "invalid_graph")

    def test_no_path_exit_code_zero(self):
        payload = json.loads(SAMPLE.read_text(encoding="utf-8"))
        payload["target"] = "__nonexistent__"
        proc = self._run(input_text=json.dumps(payload))
        self.assertEqual(proc.returncode, 2)
        err = json.loads(proc.stderr)
        self.assertEqual(err["error"], "unknown_node")

    def test_missing_file(self):
        proc = self._run(["/nonexistent/request.json"])
        self.assertEqual(proc.returncode, 1)


if __name__ == "__main__":
    unittest.main()
