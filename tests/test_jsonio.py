"""JSON 接口端到端测试：null 缺测、逐步错误、错误信封、严格 JSON。"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from kfmu.jsonio import error_envelope, process_request

ROOT = Path(__file__).resolve().parent.parent


def cv_payload(z_steps, *, R=None, masks=None):
    F = [[1.0, 1.0], [0.0, 1.0]]
    H = [[1.0, 0.0]]
    Q = [[0.0025, 0.005], [0.005, 0.01]]
    return {
        "model": {"F": F, "H": H, "Q": Q, "R": R if R is not None else [[1.0]]},
        "initial_state": {"x": [0.0, 0.0], "P": [[10.0, 0.0], [0.0, 10.0]]},
        "measurements": z_steps,
        "masks": masks,
    }


class TestJsonApi(unittest.TestCase):
    def test_full_and_missing_steps(self):
        payload = cv_payload([[1.0], None, [None], [2.0]])
        resp = process_request(payload)
        self.assertTrue(resp["ok"])
        statuses = [s["status"] for s in resp["steps"]]
        self.assertEqual(statuses, ["updated", "predicted_only", "predicted_only", "updated"])
        # 缺测步也输出全 False 的分量掩码
        self.assertEqual(resp["steps"][1]["available"], [False])
        self.assertFalse(any(s["available"][0] for s in resp["steps"][1:3]))
        # 诊断字段
        for step in resp["steps"]:
            if step["status"] != "error":
                self.assertLessEqual(
                    step["diagnostics"]["max_asymmetry"], 1e-9
                )
                self.assertGreaterEqual(
                    step["diagnostics"]["min_eigenvalue"], -1e-8
                )

    def test_mask_intersection_with_null(self):
        # 掩码说有值，但分量给了 null => 仍判缺测
        payload = cv_payload([[None]], masks=[[True]])
        resp = process_request(payload)
        self.assertEqual(resp["steps"][0]["status"], "predicted_only")

    def test_singular_innovation_is_step_error_not_fatal(self):
        # Q=0, R=0, P0=0 => S=0 奇异
        payload = cv_payload([[1.0], [2.0]], R=[[0.0]])
        payload["model"]["Q"] = [[0.0, 0.0], [0.0, 0.0]]
        payload["initial_state"]["P"] = [[0.0, 0.0], [0.0, 0.0]]
        resp = process_request(payload)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["summary"]["num_errors"], 2)
        self.assertEqual(resp["steps"][0]["error"]["code"],
                         "singular_innovation_covariance")
        # 出错步仍返回预测状态（有限数）
        self.assertTrue(all(isinstance(v, float) for v in resp["steps"][0]["x"]))

    def test_fatal_model_error_envelope(self):
        payload = cv_payload([[1.0]])
        payload["model"]["R"] = [[-1.0]]  # 负定
        try:
            process_request(payload)
            self.fail("应抛出异常")
        except Exception as exc:
            env = error_envelope(exc)
            self.assertFalse(env["ok"])
            self.assertIn(env["error"]["code"],
                          {"matrix_property_violation"})

    def test_invalid_request_shapes(self):
        with self.assertRaises(Exception):
            process_request({"model": {}})  # 缺矩阵
        payload = cv_payload([[1.0, 2.0]])  # 量测维数错
        with self.assertRaises(Exception):
            process_request(payload)

    def test_strict_json_no_nan_token(self):
        resp = process_request(cv_payload([[1.0], None]))
        text = json.dumps(resp, allow_nan=False)  # 不允许 NaN/Infinity 字面量
        self.assertNotIn("NaN", text)
        self.assertNotIn("Infinity", text)

    def test_cli_roundtrip(self):
        proc = subprocess.run(
            [sys.executable, "-m", "kfmu.cli"],
            input=json.dumps(cv_payload([[1.0], None, [2.0]])),
            capture_output=True, text=True, cwd=ROOT, check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        resp = json.loads(proc.stdout)
        self.assertEqual(resp["summary"]["num_steps"], 3)
        self.assertEqual(resp["summary"]["num_predicted_only"], 1)

    def test_cli_strict_step_errors_exit_code(self):
        payload = cv_payload([[1.0]], R=[[0.0]])
        payload["model"]["Q"] = [[0.0, 0.0], [0.0, 0.0]]
        payload["initial_state"]["P"] = [[0.0, 0.0], [0.0, 0.0]]
        proc = subprocess.run(
            [sys.executable, "-m", "kfmu.cli", "--strict-step-errors"],
            input=json.dumps(payload), capture_output=True, text=True,
            cwd=ROOT, check=False,
        )
        self.assertEqual(proc.returncode, 3)
        resp = json.loads(proc.stdout)
        self.assertEqual(resp["summary"]["num_errors"], 1)

    def test_cli_invalid_json_exit_code_2(self):
        proc = subprocess.run(
            [sys.executable, "-m", "kfmu.cli"],
            input="{not json", capture_output=True, text=True,
            cwd=ROOT, check=False,
        )
        self.assertEqual(proc.returncode, 2)
        resp = json.loads(proc.stdout)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "invalid_json")


if __name__ == "__main__":
    unittest.main()
