"""JSON 入口与 CLI 端到端测试。"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from collision_detection import build_response, load_request
from collision_detection.io_json import parse_request

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = PROJECT_ROOT / "examples"


class TestParseRequest(unittest.TestCase):
    def test_minimal_request_defaults_window(self):
        req = parse_request({
            "robot": {"center": [0, 0], "velocity": [1, 0], "radius": 1},
            "obstacles": [{"center": [5, 0], "velocity": [0, 0], "radius": 1}],
        })
        self.assertEqual((req.t_start, req.t_end), (0.0, 1.0))
        self.assertEqual(len(req.obstacles), 1)

    def test_rejects_missing_robot(self):
        with self.assertRaises(ValueError):
            parse_request({"obstacles": []})

    def test_rejects_mixed_body_description(self):
        with self.assertRaises(ValueError):
            parse_request({
                "robot": {"center": [0, 0], "velocity": [1, 0], "radius": 1,
                          "samples": [[0, 0, 0], [1, 1, 0]]},
                "obstacles": [],
            })

    def test_rejects_bad_window(self):
        with self.assertRaises(ValueError):
            parse_request({
                "time_window": [2.0, 1.0],
                "robot": {"center": [0, 0], "velocity": [1, 0], "radius": 1},
                "obstacles": [],
            })

    def test_rejects_negative_radius(self):
        with self.assertRaises(ValueError):
            parse_request({
                "robot": {"center": [0, 0], "velocity": [1, 0], "radius": -1},
                "obstacles": [],
            })


class TestExampleRequests(unittest.TestCase):
    """直接驱动 examples/ 下的请求样例，断言手算结果。"""

    def _run(self, name):
        return build_response(load_request(EXAMPLES / name))

    def test_tangent_example(self):
        resp = self._run("request_tangent.json")
        self.assertTrue(resp["success"])
        analysis = resp["data"]["obstacles"][0]["analysis"]
        self.assertTrue(analysis["collides"])
        self.assertEqual(analysis["kind"], "tangent")
        self.assertAlmostEqual(analysis["t_enter"], 2.0, places=9)

    def test_initial_overlap_example(self):
        resp = self._run("request_initial_overlap.json")
        analysis = resp["data"]["obstacles"][0]["analysis"]
        self.assertTrue(analysis["collides"])
        self.assertEqual(analysis["kind"], "overlap")
        self.assertAlmostEqual(analysis["t_enter"], 0.0, places=12)

    def test_same_velocity_example(self):
        resp = self._run("request_same_velocity.json")
        obstacles = resp["data"]["obstacles"]
        self.assertFalse(obstacles[0]["analysis"]["collides"])
        self.assertTrue(obstacles[1]["analysis"]["collides"])
        self.assertEqual(obstacles[1]["analysis"]["kind"], "overlap")

    def test_high_speed_example(self):
        resp = self._run("request_high_speed.json")
        analysis = resp["data"]["obstacles"][0]["analysis"]
        self.assertTrue(analysis["collides"])
        self.assertAlmostEqual(analysis["t_enter"], 0.50043755, places=6)

    def test_sensor_fit_example(self):
        resp = self._run("request_sensor_fit.json")
        analysis = resp["data"]["obstacles"][0]["analysis"]
        self.assertTrue(analysis["collides"])
        # 真值 t_enter=(24-√60)/8≈2.032，拟合噪声下允许 0.05 偏差
        self.assertAlmostEqual(analysis["t_enter"], 2.032, delta=0.05)


class TestCli(unittest.TestCase):
    def _cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "collision_detection.cli", *args],
            cwd=PROJECT_ROOT, capture_output=True, text=True,
        )

    def test_cli_stdout_success(self):
        proc = self._cli(str(EXAMPLES / "request_tangent.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        resp = json.loads(proc.stdout)
        self.assertTrue(resp["success"])
        self.assertTrue(resp["data"]["any_collision"])

    def test_cli_output_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "resp.json"
            proc = self._cli(str(EXAMPLES / "request_high_speed.json"),
                             "-o", str(out))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            resp = json.loads(out.read_text(encoding="utf-8"))
            self.assertTrue(resp["data"]["any_collision"])

    def test_cli_missing_file_fails_with_envelope(self):
        proc = self._cli("no_such_file.json")
        self.assertEqual(proc.returncode, 1)
        resp = json.loads(proc.stdout)
        self.assertFalse(resp["success"])
        self.assertIsNotNone(resp["error"])

    def test_cli_invalid_json_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            proc = self._cli(str(bad))
            self.assertEqual(proc.returncode, 1)
            self.assertFalse(json.loads(proc.stdout)["success"])


if __name__ == "__main__":
    unittest.main()
