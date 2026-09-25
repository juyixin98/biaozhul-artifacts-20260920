"""JSON 请求接口与 CLI 测试。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tensor_planner.api import RequestError, build_dag_from_dict, handle_request

ROOT = Path(__file__).resolve().parent.parent
REQUESTS = ROOT / "examples" / "requests"


def test_auto_time_assignment() -> None:
    spec = {
        "inputs": {"A": [2, 2], "B": [2, 2]},
        "ops": [
            {"name": "C", "op": "add", "inputs": ["A", "B"]},
            {"name": "D", "op": "matmul", "inputs": ["C", "A"]},
        ],
    }
    dag = build_dag_from_dict(spec)
    assert dag.producer["C"].time == 1
    assert dag.producer["D"].time == 2


def test_inputs_must_be_list_of_strings() -> None:
    spec = {
        "inputs": {"A": [3, 3], "B": [3, 3]},
        "ops": [
            {"name": "C", "op": "add", "inputs": ["A", "B"], "time": 1},
            {"name": "D", "op": "matmul", "inputs": "AB", "time": 2},
        ],
    }
    with pytest.raises(RequestError, match="需要恰好 2 个 inputs"):
        handle_request(spec)


def test_full_request_passes() -> None:
    spec = {
        "inputs": {"A": [3, 3], "B": [3, 3]},
        "ops": [
            {"name": "C", "op": "add", "inputs": ["A", "B"], "time": 1},
            {"name": "D", "op": "matmul", "inputs": ["C", "B"], "time": 2},
            {"name": "E", "op": "add", "inputs": ["D", "C"], "time": 3},
        ],
        "seed": 5,
        "budget_bytes": 8 * 27,
    }
    report = handle_request(spec)
    assert report["passed"]
    assert report["correctness"]["outputs_match"]
    assert report["memory"]["arena_elements"] <= 27
    assert report["memory"]["within_budget"]
    assert report["outputs"] == ["E"]
    # 报告必须可 JSON 序列化
    json.dumps(report)


def test_synthetic_feeds_are_deterministic() -> None:
    spec = {
        "inputs": {"A": [2, 2], "B": [2, 2]},
        "ops": [{"name": "C", "op": "add", "inputs": ["A", "B"], "time": 1}],
        "seed": 11,
    }
    r1 = handle_request(spec)
    r2 = handle_request(spec)
    assert r1["memory"] == r2["memory"]
    assert r1["passed"] and r2["passed"]


def test_explicit_feeds_override_synthetic() -> None:
    spec = {
        "inputs": {"A": [2], "B": [2]},
        "ops": [{"name": "C", "op": "add", "inputs": ["A", "B"], "time": 1}],
        "feeds": {"A": [1.0, 2.0], "B": [3.0, 4.0]},
    }
    report = handle_request(spec)
    assert report["passed"]


def test_feed_shape_mismatch_rejected() -> None:
    spec = {
        "inputs": {"A": [2], "B": [2]},
        "ops": [{"name": "C", "op": "add", "inputs": ["A", "B"], "time": 1}],
        "feeds": {"A": [1.0, 2.0, 3.0], "B": [3.0, 4.0]},
    }
    with pytest.raises(RequestError, match="形状"):
        handle_request(spec)


def test_unknown_op_rejected() -> None:
    spec = {
        "inputs": {"A": [2], "B": [2]},
        "ops": [{"name": "C", "op": "relu", "inputs": ["A"], "time": 1}],
    }
    with pytest.raises(RequestError, match="不受支持"):
        handle_request(spec)


def test_empty_inputs_rejected() -> None:
    with pytest.raises(RequestError, match="inputs"):
        handle_request({"inputs": {}, "ops": []})


def test_budget_must_be_positive_int() -> None:
    spec = {
        "inputs": {"A": [2], "B": [2]},
        "ops": [{"name": "C", "op": "add", "inputs": ["A", "B"], "time": 1}],
        "budget_bytes": -5,
    }
    with pytest.raises(RequestError, match="budget_bytes"):
        handle_request(spec)


def test_slice_request() -> None:
    spec = {
        "inputs": {"X": [4, 4], "P": [4, 2]},
        "ops": [
            {"name": "v", "op": "slice", "src": "X",
             "starts": [0, 0], "sizes": [2, 4], "time": 1},
            {"name": "w", "op": "slice", "src": "X",
             "starts": [2, 0], "sizes": [2, 4], "time": 2},
            {"name": "A", "op": "matmul", "inputs": ["v", "P"], "time": 3},
            {"name": "B", "op": "matmul", "inputs": ["w", "P"], "time": 4},
        ],
    }
    report = handle_request(spec)
    assert report["passed"]
    assert report["memory"]["arena_elements"] == 28
    # 视图成员必须出现在 placements 的 members 里
    members = {
        p["root"]: p["members"] for p in report["placements"]
    }
    assert members["X"] == ["X", "v", "w"]


@pytest.mark.parametrize(
    "request_file",
    sorted(REQUESTS.glob("*.json")),
    ids=lambda p: p.name,
)
def test_example_requests_all_pass(request_file: Path) -> None:
    spec = json.loads(request_file.read_text(encoding="utf-8"))
    report = handle_request(spec)
    assert report["passed"], report["errors"]
    json.dumps(report)


def test_cli_run(tmp_path: Path) -> None:
    request = REQUESTS / "01_chain.json"
    out = tmp_path / "report.json"
    proc = subprocess.run(
        [sys.executable, "-m", "tensor_planner.cli", "run",
         str(request), "-o", str(out)],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["passed"]


def test_cli_invalid_request_exit_code(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"inputs": {}, "ops": []}), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "tensor_planner.cli", "run", str(bad)],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
        cwd=str(ROOT),
    )
    assert proc.returncode == 2
    assert "请求无效" in proc.stderr
