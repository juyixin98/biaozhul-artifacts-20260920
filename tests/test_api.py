"""JSON 接口校验、失败状态与边界条件测试。"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from mcf.api import MCFError, error_response, process_json_text, solve_request
from mcf.solver import NegativeCycleError  # noqa: F401（文档化依赖）

ROOT = Path(__file__).resolve().parent.parent


def _basic_request(**overrides) -> dict:
    req = {
        "n": 3,
        "source": 0,
        "sink": 2,
        "edges": [
            {"from": 0, "to": 1, "capacity": 3, "cost": 2},
            {"from": 1, "to": 2, "capacity": 3, "cost": 1},
        ],
    }
    req.update(overrides)
    return req


class JsonApiTests(unittest.TestCase):
    def test_basic_max_flow_response(self) -> None:
        resp = solve_request(_basic_request())
        self.assertEqual(resp["status"], "ok")
        r = resp["result"]
        self.assertEqual(r["status"], "optimal")
        self.assertEqual(r["flow"], 3)
        self.assertEqual(r["cost"], 9)
        self.assertTrue(r["max_flow_reached"])
        self.assertEqual([f["flow"] for f in r["flows"]], [3, 3])
        # 势函数：0->1 费用2，1->2 费用1
        pot = r["potentials"]
        self.assertEqual(pot[0], 0)
        self.assertEqual(pot[1], 2)
        self.assertEqual(pot[2], 3)

    def test_required_flow_exact(self) -> None:
        resp = solve_request(_basic_request(required_flow=2))
        r = resp["result"]
        self.assertEqual(r["status"], "optimal")
        self.assertEqual(r["flow"], 2)
        self.assertEqual(r["cost"], 6)
        self.assertFalse(r["max_flow_reached"])

    def test_required_flow_too_large_is_infeasible(self) -> None:
        resp = solve_request(_basic_request(required_flow=5))
        r = resp["result"]
        self.assertEqual(r["status"], "infeasible")
        self.assertEqual(r["flow"], 3)  # 已增广部分仍然返回
        self.assertEqual(r["cost"], 9)

    def test_zero_required_flow(self) -> None:
        r = solve_request(_basic_request(required_flow=0))["result"]
        self.assertEqual((r["status"], r["flow"], r["cost"]), ("optimal", 0, 0))

    def test_no_edges_unreachable_sink(self) -> None:
        req = {"n": 3, "source": 0, "sink": 2, "edges": []}
        r = solve_request(req)["result"]
        self.assertEqual(r["status"], "optimal")
        self.assertEqual((r["flow"], r["cost"]), (0, 0))

    # ---- 输入校验 -----------------------------------------------------------
    def _expect_invalid(self, payload):
        with self.assertRaises(MCFError) as ctx:
            solve_request(payload)
        self.assertEqual(ctx.exception.code, "invalid_request")
        return ctx.exception.message

    def test_invalid_types_rejected(self) -> None:
        self._expect_invalid(_basic_request(n="3"))
        self._expect_invalid(_basic_request(n=True))       # bool 不算整数
        self._expect_invalid(_basic_request(edges={}))
        bad_edge = {"from": 0, "to": 1, "capacity": 3.0, "cost": 1}
        self._expect_invalid(_basic_request(edges=[bad_edge]))
        bad_edge2 = {"from": 0, "to": 1, "capacity": "3", "cost": 1}
        self._expect_invalid(_basic_request(edges=[bad_edge2]))

    def test_missing_fields(self) -> None:
        msg = self._expect_invalid({"source": 0, "sink": 1})
        self.assertIn("n", msg)
        self._expect_invalid(
            {"n": 2, "source": 0, "sink": 1, "edges": [{"from": 0}]}
        )

    def test_out_of_range(self) -> None:
        self._expect_invalid(_basic_request(n=1))          # 源汇相同/不足
        self._expect_invalid(_basic_request(source=5))     # 越界
        big_cap = [{"from": 0, "to": 1, "capacity": 10**10, "cost": 0}]
        self._expect_invalid(
            {"n": 2, "source": 0, "sink": 1, "edges": big_cap}
        )
        big_cost = [{"from": 0, "to": 1, "capacity": 1, "cost": 10**7}]
        self._expect_invalid(
            {"n": 2, "source": 0, "sink": 1, "edges": big_cost}
        )
        neg_cap = [{"from": 0, "to": 1, "capacity": -1, "cost": 0}]
        self._expect_invalid(
            {"n": 2, "source": 0, "sink": 1, "edges": neg_cap}
        )

    def test_negative_cycle_detected(self) -> None:
        # 0->1(-2), 1->0(1)：环费用 -1，容量均 > 0 ⇒ 可达负环。
        req = {
            "n": 3,
            "source": 0,
            "sink": 2,
            "edges": [
                {"from": 0, "to": 1, "capacity": 5, "cost": -2},
                {"from": 1, "to": 0, "capacity": 5, "cost": 1},
                {"from": 0, "to": 2, "capacity": 1, "cost": 0},
            ],
        }
        with self.assertRaises(MCFError) as ctx:
            solve_request(req)
        self.assertEqual(ctx.exception.code, "negative_cycle")

    def test_zero_capacity_edges_cannot_form_cycle(self) -> None:
        # 同样的拓扑但反向边容量 0：不是负环，正常求解。
        req = {
            "n": 3,
            "source": 0,
            "sink": 2,
            "edges": [
                {"from": 0, "to": 1, "capacity": 5, "cost": -2},
                {"from": 1, "to": 0, "capacity": 0, "cost": 1},
                {"from": 0, "to": 2, "capacity": 1, "cost": 0},
            ],
        }
        r = solve_request(req)["result"]
        self.assertEqual((r["flow"], r["cost"]), (1, 0))


class ProcessJsonTextTests(unittest.TestCase):
    def test_valid_text(self) -> None:
        text = json.dumps(_basic_request())
        resp, code = process_json_text(text)
        self.assertEqual(code, 0)
        self.assertEqual(resp["status"], "ok")

    def test_malformed_json(self) -> None:
        resp, code = process_json_text("{not json")
        self.assertEqual(code, 2)
        self.assertEqual(resp["error"]["code"], "invalid_json")

    def test_business_error_payload(self) -> None:
        resp, code = process_json_text(json.dumps({"n": 1}))
        self.assertEqual(code, 2)
        self.assertEqual(resp["status"], "error")

    def test_error_response_shape(self) -> None:
        resp = error_response("negative_cycle", "boom")
        self.assertEqual(resp, {
            "status": "error",
            "error": {"code": "negative_cycle", "message": "boom"},
        })


class CliTests(unittest.TestCase):
    """通过子进程实际运行 ``python -m mcf.cli``。"""

    def _run(self, text: str) -> tuple[int, dict]:
        proc = subprocess.run(
            [sys.executable, "-m", "mcf.cli"],
            input=text,
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )
        return proc.returncode, json.loads(proc.stdout)

    def test_cli_stdin_success(self) -> None:
        code, resp = self._run(json.dumps(_basic_request()))
        self.assertEqual(code, 0)
        self.assertEqual(resp["result"]["flow"], 3)

    def test_cli_stdin_error_exit_code(self) -> None:
        code, resp = self._run("{bad")
        self.assertEqual(code, 2)
        self.assertEqual(resp["error"]["code"], "invalid_json")

    def test_cli_file_argument(self) -> None:
        sample = ROOT / "examples" / "01_basic.json"
        if not sample.exists():
            self.skipTest("样例文件尚未生成")
        proc = subprocess.run(
            [sys.executable, "-m", "mcf.cli", str(sample)],
            capture_output=True, text=True, cwd=ROOT, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        resp = json.loads(proc.stdout)
        self.assertEqual(resp["status"], "ok")


if __name__ == "__main__":
    unittest.main()
