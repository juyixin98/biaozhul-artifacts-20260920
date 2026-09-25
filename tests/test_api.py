"""JSON 接口与输入校验测试。"""

import json
import subprocess
import sys

import pytest

from knapsack.api import solve_request
from knapsack.validate import MAX_ITEMS


def solve_ok(req):
    resp = solve_request(req)
    assert resp["status"] in ("optimal", "feasible"), resp
    return resp


class TestHappyPath:
    def test_basic_request(self):
        resp = solve_request(
            {
                "capacity": 8,
                "items": [
                    {"id": "a", "weight": 3, "value": 6},
                    {"id": "b", "weight": 4, "value": 7},
                    {"id": "c", "weight": 5, "value": 9},
                ],
            }
        )
        assert resp["status"] == "optimal"
        assert resp["value"] == 15
        assert resp["upper_bound"] == 15
        assert resp["gap"] == 0
        assert sorted(resp["selected_ids"]) == ["a", "c"]
        assert resp["total_weight"] == 8

    def test_default_ids(self):
        resp = solve_request(
            {"capacity": 2, "items": [{"weight": 1, "value": 1}, {"weight": 5, "value": 9}]}
        )
        assert resp["status"] == "optimal"
        assert resp["selected_ids"] == ["item-0"]

    def test_feasible_status_on_tiny_limits(self):
        items = [{"weight": (i % 7) + 1, "value": (i * 3) % 11 + 1} for i in range(60)]
        resp = solve_request(
            {"capacity": 100, "items": items, "options": {"time_limit_ms": 1, "max_nodes": 10}}
        )
        assert resp["status"] == "feasible"
        assert resp["gap"] > 0
        assert resp["upper_bound"] > resp["value"]
        assert "gap not closed" in resp["message"]

    def test_empty_items(self):
        resp = solve_request({"capacity": 5, "items": []})
        assert resp["status"] == "optimal"
        assert resp["value"] == 0


class TestValidationErrors:
    @pytest.mark.parametrize(
        "req, fragment",
        [
            ({"items": []}, "capacity"),
            ({"capacity": -1, "items": []}, "capacity"),
            ({"capacity": 1.5, "items": []}, "integer"),
            ({"capacity": True, "items": []}, "integer"),
            ({"capacity": 5, "items": [{"weight": 1}]}, "value"),
            ({"capacity": 5, "items": [{"weight": -1, "value": 1}]}, "weight"),
            ({"capacity": 5, "items": [{"weight": 1.0, "value": 1}]}, "integer"),
            ({"capacity": 5, "items": [{"weight": 1, "value": 2.5}]}, "integer"),
            (
                {
                    "capacity": 5,
                    "items": [
                        {"id": "x", "weight": 1, "value": 1},
                        {"id": "x", "weight": 2, "value": 2},
                    ],
                },
                "duplicate",
            ),
            ({"capacity": 5, "items": [{"weight": 1, "value": 1, "id": 3}]}, "string"),
            (
                {"capacity": 5, "items": [{"weight": 1, "value": 1}], "options": {"time_limit_ms": 0}},
                "time_limit_ms",
            ),
            (
                {
                    "capacity": 5,
                    "items": [{"weight": 1, "value": 1}],
                    "options": {"max_nodes": 10**9},
                },
                "max_nodes",
            ),
            ("not-a-dict", "object"),
            ({"capacity": 5, "items": {}}, "array"),
        ],
    )
    def test_invalid_requests(self, req, fragment):
        resp = solve_request(req)
        assert resp["status"] == "error"
        assert resp["error"]["code"] == "invalid_input"
        assert fragment in resp["error"]["message"]

    def test_too_many_items(self):
        resp = solve_request(
            {"capacity": 5, "items": [{"weight": 1, "value": 1}] * (MAX_ITEMS + 1)}
        )
        assert resp["status"] == "error"
        assert str(MAX_ITEMS) in resp["error"]["message"]


class TestCli:
    def test_cli_roundtrip(self, tmp_path):
        req = {
            "capacity": 8,
            "items": [
                {"id": "a", "weight": 3, "value": 6},
                {"id": "b", "weight": 4, "value": 7},
                {"id": "c", "weight": 5, "value": 9},
            ],
        }
        path = tmp_path / "req.json"
        path.write_text(json.dumps(req), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "knapsack", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        resp = json.loads(proc.stdout)
        assert resp["status"] == "optimal"
        assert resp["value"] == 15

    def test_cli_invalid_input_exit_code(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"capacity": -1}), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "knapsack", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 1
        assert json.loads(proc.stdout)["status"] == "error"
