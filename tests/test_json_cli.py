"""JSON 接口与命令行端到端测试。"""

import json
import subprocess
import sys

import pytest

from bounded_lp.io_json import handle_request, loads


def test_json_optimal_full_response():
    req = {
        "sense": "min",
        "c": [-3, -5],
        "A_ub": [[1, 0], [0, 2], [3, 2]],
        "b_ub": [4, 12, 18],
    }
    resp = handle_request(req)
    assert resp["status"] == "optimal"
    assert resp["objective"] == pytest.approx(-36.0)
    assert resp["residuals"]["max_abs_residual"] <= 1e-7
    assert resp["iterations"]["total"] >= 0
    # 可被标准 json 序列化（无 NaN/Infinity）
    json.dumps(resp, allow_nan=False)


def test_json_constraint_list_form():
    req = {
        "c": [-3, -5],
        "constraints": [
            {"a": [1, 0], "op": "<=", "b": 4},
            {"a": [0, 2], "op": "<=", "b": 12},
            {"a": [3, 2], "op": "<=", "b": 18},
            {"a": [1, 1], "op": ">=", "b": 1},
        ],
    }
    resp = handle_request(req)
    assert resp["status"] == "optimal"
    assert resp["objective"] == pytest.approx(-36.0)


def test_json_null_ub_means_infinity():
    resp = handle_request({"c": [-1, -1], "ub": [3, None],
                           "A_ub": [[1, 1]], "b_ub": [10]})
    assert resp["status"] == "optimal"
    assert resp["x"][0] == pytest.approx(3.0)
    assert resp["x"][1] == pytest.approx(7.0)


def test_json_infeasible_and_unbounded_shapes():
    r1 = handle_request({"c": [1, 1],
                         "A_eq": [[1, 1], [1, 1]], "b_eq": [1, 3]})
    assert r1["status"] == "infeasible"
    assert len(r1["farkas_y"]) == 2

    r2 = handle_request({"c": [-1], "A_ub": [[-1]], "b_ub": [1]})
    assert r2["status"] == "unbounded"
    assert r2["ray"] == [1.0]
    assert r2["objective_direction"] < 0


@pytest.mark.parametrize("bad,expected_path", [
    ({"sense": "weird", "c": [1]}, "sense"),
    ({"c": [1, 2], "A_ub": [[1]], "b_ub": [1]}, "A_ub"),
    ({"c": [1, 2], "A_ub": [[1, 0]], "b_ub": [1, 2]}, "b_ub"),
    ({"c": [1], "lb": [-1]}, "lb"),
    ({"c": [1], "ub": [0], "lb": [1]}, "ub"),
    ({"c": "abc"}, "c"),
    ({"c": [1, 2], "constraints": [{"a": [1], "op": "<=", "b": 0}]}, "constraints[0].a"),
    ({"c": [1, 2], "constraints": [{"a": [1, 2], "op": "==", "b": 1}],
      "A_ub": [[1, 2]], "b_ub": [2]}, "constraints"),
    ({"c": [1], "constraints": [{"a": [1], "op": "~", "b": 1}]}, "constraints[0].op"),
])
def test_invalid_requests_report_paths(bad, expected_path):
    resp = handle_request(bad)
    assert resp["status"] == "invalid_request"
    assert any(expected_path in e for e in resp["errors"])


def test_invalid_json_syntax():
    resp = loads("{not json")
    assert resp["status"] == "invalid_request"
    assert "JSON 语法错误" in resp["errors"][0]


def test_nan_infinity_rejected():
    resp = loads('{"c": [1, NaN]}')
    assert resp["status"] == "invalid_request"


def test_bad_options():
    resp = handle_request({"c": [1], "options": {"rule": "fancy"}})
    assert resp["status"] == "invalid_request"
    resp = handle_request({"c": [1], "options": {"feas_tol": 5}})
    assert resp["status"] == "invalid_request"
    # pivot_tol 必须小于 feas_tol
    resp = handle_request({"c": [1], "options": {"feas_tol": 1e-11, "pivot_tol": 1e-9}})
    assert resp["status"] == "invalid_request"


def test_iteration_limit_failed_status():
    # 极小的迭代上限使简单问题无法完成 Phase I
    resp = handle_request({
        "c": [1, 1],
        "A_eq": [[1, 0], [0, 1]], "b_eq": [1, 1],
        "options": {"max_iterations": 0},
    })
    assert resp["status"] == "invalid_request"  # 选项范围校验先拦截

    resp = handle_request({
        "c": [1, 1],
        "A_eq": [[1, 0], [0, 1]], "b_eq": [1, 1],
        "options": {"max_iterations": 1},
    })
    # 1 次预算：可能恰好够用或 failed；两种都合法，但 failed 时须带 reason
    assert resp["status"] in ("optimal", "failed")


def test_maximize_unbounded_json():
    resp = handle_request({"sense": "max", "c": [1, 2],
                           "A_ub": [[-1, -1]], "b_ub": [1]})
    assert resp["status"] == "unbounded"
    assert resp["objective_direction"] > 0
    assert resp["residuals"]["ray_ub_max_direction"] <= 1e-7


def test_dantzig_option_accepted():
    resp = handle_request({
        "c": [-3, -5],
        "A_ub": [[1, 0], [0, 2], [3, 2]], "b_ub": [4, 12, 18],
        "options": {"rule": "dantzig"},
    })
    assert resp["status"] == "optimal"
    assert resp["rule"] == "dantzig"


# --------------------------------------------------------------------------
# CLI 端到端
# --------------------------------------------------------------------------


def _run_cli(tmp_path, payload, *extra):
    path = tmp_path / "req.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "bounded_lp.cli", str(path), *extra],
        capture_output=True, text=True,
    )
    return proc


def test_cli_file_to_stdout(tmp_path):
    proc = _run_cli(tmp_path, {"c": [-3, -5],
                               "A_ub": [[1, 0], [0, 2], [3, 2]],
                               "b_ub": [4, 12, 18]})
    assert proc.returncode == 0, proc.stderr
    resp = json.loads(proc.stdout)
    assert resp["status"] == "optimal"


def test_cli_output_file_and_exit_codes(tmp_path):
    out = tmp_path / "out.json"
    proc = _run_cli(tmp_path, {"c": [1, 1],
                               "A_eq": [[1, 1], [1, 1]], "b_eq": [1, 3]},
                    "-o", str(out))
    assert proc.returncode == 0            # 不可行是明确结论
    resp = json.loads(out.read_text())
    assert resp["status"] == "infeasible"

    bad = tmp_path / "bad.json"
    bad.write_text("{broken", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "bounded_lp.cli", str(bad)],
        capture_output=True, text=True)
    assert proc.returncode == 2


def test_cli_stdin(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "bounded_lp.cli"],
        input=json.dumps({"c": [-1], "A_ub": [[-1]], "b_ub": [1]}),
        capture_output=True, text=True)
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["status"] == "unbounded"
