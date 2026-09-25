"""输入校验与 JSON/CLI 接口测试：失败状态、输入范围、容差边界。"""

from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from knapsack.api import solve  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _expect_error(payload, field=None):
    resp = solve(payload)
    assert resp["ok"] is False, resp
    assert resp["error"]["code"] == "validation_error"
    if field is not None:
        assert resp["error"]["field"] == field, resp
    return resp


def test_validation_errors():
    # 合法对照
    ok = solve({"capacity": 10, "weights": [1], "values": [1]})
    assert ok["ok"] is True

    _expect_error({}, field="capacity")
    _expect_error({"capacity": 10, "weights": [1]}, field="values")
    _expect_error({"capacity": 10, "weights": [1], "values": [1, 2]}, field="values")
    _expect_error({"capacity": -1, "weights": [], "values": []}, field="capacity")
    _expect_error({"capacity": 0, "weights": [-1], "values": [1]}, field="weights[0]")
    _expect_error({"capacity": 0, "weights": [1.5], "values": [1]}, field="weights[0]")
    _expect_error({"capacity": 0, "weights": [1], "values": ["x"]}, field="values[0]")
    _expect_error({"capacity": 0, "weights": [1], "values": [True]}, field="values[0]")
    _expect_error({"capacity": 0, "weights": "ab", "values": []}, field="weights")
    _expect_error([1, 2, 3], field="<body>")
    _expect_error({"capacity": 0, "weights": [1], "values": [10**9 + 1]}, field="values[0]")
    _expect_error({"capacity": 0, "weights": [10**9 + 1], "values": [1]}, field="weights[0]")

    # 规模上限
    big = {"capacity": 1, "weights": [1] * 2001, "values": [1] * 2001}
    _expect_error(big, field="weights")

    # timeout 范围
    _expect_error({"capacity": 1, "weights": [1], "values": [1],
                   "timeout_seconds": 0}, field="timeout_seconds")
    _expect_error({"capacity": 1, "weights": [1], "values": [1],
                   "timeout_seconds": 10**4}, field="timeout_seconds")
    _expect_error({"capacity": 1, "weights": [1], "values": [1],
                   "timeout_seconds": "1"}, field="timeout_seconds")
    print("test_validation_errors: 通过")


def test_boundary_sizes_accepted():
    """边界规模：n=0、n=2000、容量上限均可求解。"""

    r0 = solve({"capacity": 0, "weights": [], "values": []})
    assert r0["ok"] and r0["objective"] == 0

    n = 2000
    r = solve({
        "capacity": 10**12,
        "weights": [1] * n,
        "values": [1] * n,
        "timeout_seconds": 10.0,
    })
    assert r["ok"] and r["status"] == "optimal" and r["objective"] == n
    assert r["total_weight"] == n
    print("test_boundary_sizes_accepted: n=2000 边界实例通过")


def test_cli_file_and_stdin_and_bad_json():
    """CLI：文件输入、stdin 管道、非法 JSON 退出码 2。"""

    req = {"capacity": 10, "weights": [6, 4, 3], "values": [10, 7, 6]}
    # stdin
    p = subprocess.run(
        [sys.executable, "-m", "knapsack.cli"],
        input=json.dumps(req), capture_output=True, text=True, cwd=ROOT,
    )
    assert p.returncode == 0, p.stderr
    resp = json.loads(p.stdout)
    # (w=6,v=10)+(w=4,v=7) 恰好装满容量 10，价值 17 为最优
    assert resp["objective"] == 17 and resp["status"] == "optimal", resp

    # 非法 JSON
    p2 = subprocess.run(
        [sys.executable, "-m", "knapsack.cli"],
        input="{not json", capture_output=True, text=True, cwd=ROOT,
    )
    assert p2.returncode == 2
    err = json.loads(p2.stdout)
    assert err["ok"] is False and err["error"]["code"] == "invalid_json"

    # 校验失败也是退出码 2
    p3 = subprocess.run(
        [sys.executable, "-m", "knapsack.cli"],
        input=json.dumps({"capacity": -1, "weights": [], "values": []}),
        capture_output=True, text=True, cwd=ROOT,
    )
    assert p3.returncode == 2
    assert json.loads(p3.stdout)["error"]["field"] == "capacity"
    print("test_cli_file_and_stdin_and_bad_json: 通过")


def test_cli_example_file():
    """examples/ 下的样例文件必须能真实跑通。"""

    examples = os.path.join(ROOT, "examples")
    for name in sorted(os.listdir(examples)):
        if not name.endswith(".json") or "invalid" in name:
            continue
        p = subprocess.run(
            [sys.executable, "-m", "knapsack.cli", "-f", os.path.join(examples, name)],
            capture_output=True, text=True, cwd=ROOT,
        )
        assert p.returncode == 0, (name, p.stderr)
        resp = json.loads(p.stdout)
        assert resp["ok"] is True, (name, resp)
    print("test_cli_example_file: examples/ 全部样例运行通过")
