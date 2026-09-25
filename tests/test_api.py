"""请求校验、JSON 接口与 CLI 端到端测试。"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from adaptive_integration import integrate, integrate_request
from adaptive_integration.io_layer import result_to_dict

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# 参数校验
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "kwargs",
    [
        {"abs_tol": 0.0},                       # abs_tol 过小
        {"abs_tol": -1e-10},
        {"abs_tol": 2.0},
        {"rel_tol": -1e-9},
        {"rel_tol": 1.5},
        {"max_depth": 0},
        {"max_depth": 101},
        {"max_evaluations": 10},                # 小于 15
        {"max_evaluations": 2_000_000},
        {"initial_intervals": 0},
        {"method": "monte_carlo"},
        {"a": 0.0, "b": float("inf")},
        {"a": 0.0, "b": float("nan")},
        {"a": 1e200, "b": 1e200 + 1},           # 区间过宽/端点过大
        {"points": [2.0]},                      # 分点不在区间内
        {"points": [0.4, 0.4]},                 # 重复分点
    ],
)
def test_invalid_parameters_rejected(kwargs):
    base = dict(expression="x", a=0.0, b=1.0)
    base.update(kwargs)
    r = integrate(**base)
    assert not r.converged
    assert r.error_code == "INVALID_REQUEST"
    assert r.error_message


def test_missing_expression():
    r = integrate_request({"a": 0, "b": 1})
    assert not r.converged
    assert r.error_code == "INVALID_REQUEST"
    assert "expression" in r.error_message


def test_parse_error_status():
    r = integrate("foo(x)", 0, 1)
    assert not r.converged
    assert r.error_code == "PARSE_ERROR"


def test_body_must_be_object():
    r = integrate_request([1, 2, 3])  # type: ignore[arg-type]
    assert not r.converged
    assert r.error_code == "INVALID_REQUEST"


def test_unknown_extra_fields_ignored():
    r = integrate_request(
        {"expression": "x", "a": 0, "b": 1, "comment": "hi"}
    )
    assert r.converged


def test_result_dict_is_json_serializable():
    r = integrate("exp(-x^2)", -1, 1)
    d = result_to_dict(r)
    s = json.dumps(d, ensure_ascii=False)
    d2 = json.loads(s)
    assert d2["status"] == "converged"
    assert isinstance(d2["result"], float)


def test_failed_result_dict_is_json_serializable():
    r = integrate("1/x", -1, 1)
    d = result_to_dict(r)
    json.loads(json.dumps(d, ensure_ascii=False))
    assert d["error_code"] == "INVALID_VALUE_AT_POINT"
    assert d["diagnostics"]["location"] == 0.0


# --------------------------------------------------------------------------
# CLI 端到端
# --------------------------------------------------------------------------

def _run_cli(payload, use_file=False, tmp_path=None):
    if use_file:
        p = tmp_path / "req.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "adaptive_integration.cli", "-f", str(p)],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
    else:
        proc = subprocess.run(
            [sys.executable, "-m", "adaptive_integration.cli"],
            input=json.dumps(payload),
            capture_output=True, text=True,
            cwd=REPO_ROOT,
        )
    return proc


def test_cli_converged_exit_0(tmp_path):
    proc = _run_cli({"expression": "x^2", "a": 0, "b": 1}, tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stderr
    d = json.loads(proc.stdout)
    assert d["status"] == "converged"
    assert d["result"] == pytest.approx(1 / 3, abs=1e-10)


def test_cli_file_input_exit_0(tmp_path):
    proc = _run_cli(
        {"expression": "4/(1+x^2)", "a": 0, "b": 1},
        use_file=True, tmp_path=tmp_path,
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["result"] == pytest.approx(
        3.14159265358979, abs=1e-9
    )


def test_cli_failure_exit_1(tmp_path):
    proc = _run_cli({"expression": "1/x^2", "a": 0, "b": 1}, tmp_path=tmp_path)
    assert proc.returncode == 1
    d = json.loads(proc.stdout)
    assert d["status"] == "failed"
    assert d["error_code"] in (
        "ROUND_OFF_NO_PROGRESS",
        "DEPTH_LIMIT_REACHED",
        "EVALUATION_BUDGET_EXHAUSTED",
    )


def test_cli_bad_json_exit_2(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "adaptive_integration.cli"],
        input="{not json",
        capture_output=True, text=True,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 2
    d = json.loads(proc.stdout)
    assert d["error_code"] == "INVALID_REQUEST"


def test_cli_parse_error_exit_2(tmp_path):
    proc = _run_cli({"expression": "x @@", "a": 0, "b": 1}, tmp_path=tmp_path)
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["error_code"] == "PARSE_ERROR"
