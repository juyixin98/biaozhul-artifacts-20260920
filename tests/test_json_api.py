"""JSON 接口、输入校验与 CLI 行为测试。"""

from __future__ import annotations

import json
import subprocess
import sys

import numpy as np
import pytest

from blp import run_request
from blp.errors import LPInputError
from blp.jsonio import parse_request
from blp.model import MAX_CONSTRAINTS, MAX_VARIABLES


def test_matrix_form_optimal():
    resp = run_request({
        "objective": {"c": [-3, -5]},
        "constraints": {
            "A_ub": [[1, 0], [0, 2], [3, 2]],
            "b_ub": [4, 12, 18],
        },
    })
    assert resp["status"] == "optimal"
    assert abs(resp["objective"] + 36) < 1e-9
    assert resp["residuals"]["feasible"] is True


def test_row_form_all_senses():
    resp = run_request({
        "objective": {"c": [1, 1], "sense": "min"},
        "constraint_rows": [
            {"row": [1, 1], "sense": ">=", "rhs": 1},
            {"row": [1, 0], "sense": "<=", "rhs": 0.7},
            {"row": [0, 1], "sense": "=", "rhs": 0.3},
        ],
    })
    assert resp["status"] == "optimal"
    assert abs(resp["objective"] - 1.0) < 1e-9


def test_max_sense():
    resp = run_request({
        "objective": {"c": [3, 5], "sense": "max"},
        "constraints": {
            "A_ub": [[1, 0], [0, 2], [3, 2]],
            "b_ub": [4, 12, 18],
        },
    })
    assert resp["status"] == "optimal"
    assert abs(resp["objective"] - 36) < 1e-9


def test_infeasible_response_certificate_checked():
    resp = run_request({
        "objective": {"c": [1]},
        "constraints": {"A_eq": [[1], [1]], "b_eq": [1, 2]},
    })
    assert resp["status"] == "infeasible"
    chk = resp["certificate"]["independent_check"]
    assert chk["valid"] is True
    assert chk["muTb"] < 0


def test_unbounded_response_ray():
    resp = run_request({
        "objective": {"c": [-2, 1]},
        "constraint_rows": [{"row": [1, -1], "sense": "<=", "rhs": 5}],
    })
    assert resp["status"] == "unbounded"
    assert resp["residuals"]["valid"] is True
    assert resp["ray_objective_rate"] < 0


def test_variables_bounds_fixed_feasible():
    # x1 固定为 1（lb=ub=1），x0 无上界；min x0 - x1 -> -1，x0=0。
    resp = run_request({
        "objective": {"c": [1, -1]},
        "variables": {"lb": [0, 1], "ub": [None, 1]},
    })
    assert resp["status"] == "optimal"
    assert abs(resp["objective"] - (-1)) < 1e-9
    assert abs(resp["x"][0]) < 1e-9 and abs(resp["x"][1] - 1) < 1e-9


def test_empty_box_reported_infeasible():
    # lb > ub 不是输入格式错误，而是数值上不可行的问题。
    resp = run_request({
        "objective": {"c": [1]},
        "variables": {"lb": [3], "ub": [1]},
    })
    assert resp["status"] == "infeasible"
    assert resp["certificate"]["independent_check"]["valid"] is True


@pytest.mark.parametrize("bad_req,msg", [
    ({"objective": {"c": []}}, "非空数组"),
    ({"objective": {"c": [1, "x"]}}, "数值"),
    ({"objective": {"c": [1], "sense": "mid"}}, "sense"),
    ({"objective": {"c": [1]},
      "constraints": {"A_ub": [[1, 0]], "b_ub": [1]}}, "长度"),
    ({"objective": {"c": [1]},
      "constraint_rows": [{"row": [1], "sense": "~", "rhs": 0}]}, "sense"),
    ({"objective": {"c": [1]},
      "variables": {"lb": [-1]}}, "非负"),
])
def test_invalid_inputs(bad_req, msg):
    with pytest.raises(LPInputError) as exc:
        parse_request(bad_req)
    assert msg in str(exc.value)


def test_size_limits_enforced():
    n = MAX_VARIABLES + 1
    with pytest.raises(LPInputError):
        parse_request({"objective": {"c": [0] * n}})
    m = MAX_CONSTRAINTS + 1
    with pytest.raises(LPInputError):
        parse_request({
            "objective": {"c": [1]},
            "constraints": {
                "A_ub": [[0]] * m, "b_ub": [0] * m,
            },
        })


def test_coefficient_range_rejected():
    big = 1.0e10
    with pytest.raises(LPInputError):
        parse_request({
            "objective": {"c": [big]},
        })


def test_json_must_be_object():
    with pytest.raises(LPInputError):
        parse_request([1, 2, 3])


def test_null_upper_bound_means_infinite():
    parsed = parse_request({
        "objective": {"c": [-1]},
        "variables": {"ub": None},
    })
    assert all(np.isinf(v) for v in parsed["ub"])


def test_cli_exit_codes(tmp_path):
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps({
        "objective": {"c": [-1]},
        "constraint_rows": [{"row": [1], "sense": "<=", "rhs": 1}],
    }))
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    inf = tmp_path / "inf.json"
    inf.write_text(json.dumps({
        "objective": {"c": [1]},
        "constraints": {"A_eq": [[1], [1]], "b_eq": [1, 2]},
    }))

    def run(path):
        proc = subprocess.run(
            [sys.executable, "-m", "blp.cli", str(path)],
            capture_output=True, text=True,
            env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
        )
        return proc.returncode, json.loads(proc.stdout)

    code, resp = run(ok)
    assert code == 0 and resp["status"] == "optimal"
    code, resp = run(inf)
    assert code == 0 and resp["status"] == "infeasible"

    proc = subprocess.run(
        [sys.executable, "-m", "blp.cli", str(bad)],
        capture_output=True, text=True,
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["status"] == "invalid_input"


def test_cli_stdin_stdout():
    # min -x 无约束 => 无界；同时验证标准输入/输出通路。
    req = json.dumps({"objective": {"c": [-1]}})
    proc = subprocess.run(
        [sys.executable, "-m", "blp.cli"],
        input=req, capture_output=True, text=True,
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
    )
    resp = json.loads(proc.stdout)
    assert proc.returncode == 0
    assert resp["status"] == "unbounded"
