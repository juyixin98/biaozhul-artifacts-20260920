"""End-to-end service + CLI tests on the shipped example requests."""

import json
import os
import subprocess
import sys

import pytest

from tmem.dag import DAGValidationError
from tmem.planner import BudgetExceeded
from tmem.service import handle_request

EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "examples")


def _load(name):
    with open(os.path.join(EXAMPLES, name), encoding="utf-8") as fh:
        return json.load(fh)


@pytest.mark.parametrize(
    "filename",
    [
        "multi_consumer.json",
        "shared_view.json",
        "branch_merge.json",
        "peak_budget_chain.json",
    ],
)
def test_service_reports_are_safe_and_equivalent(filename):
    report = handle_request(_load(filename))
    assert report["status"] == "ok"
    assert report["safety"]["simultaneously_live_windows_disjoint"]
    assert report["safety"]["overlapping_live_pairs"] == []
    assert report["comparison"]["match"]
    ex = report["execution"]
    assert ex["reuse"]["peak_elements"] <= ex["no_reuse"]["peak_elements"]
    assert ex["reuse"]["allocations"] == 1
    if report["budget"] is not None:
        assert report["budget"]["within_budget"]


def test_multi_consumer_report_contents():
    report = handle_request(_load("multi_consumer.json"))
    t1_buffer = next(b for b in report["plan"]["buffers"] if b["root"] == "t1")
    # t1 has two consumers at later points (t2, t3); its window must stay
    # live across both, so last_use strictly follows its birth.
    assert t1_buffer["last_use"] > t1_buffer["birth"]


def test_shared_view_report_family():
    report = handle_request(_load("shared_view.json"))
    x_buffer = next(b for b in report["plan"]["buffers"] if b["root"] == "X")
    assert set(x_buffer["alias_family"]) == {"X", "v", "v2", "v3"}
    views = {v["name"]: v for v in report["plan"]["views"]}
    assert set(views) == {"v", "v2", "v3"}
    # All view windows are inside the root arena window.
    for v in views.values():
        assert v["absolute_window"][0] >= x_buffer["window"][0]
        assert v["absolute_window"][1] <= x_buffer["window"][1]


def test_branch_merge_reuses_and_matches():
    report = handle_request(_load("branch_merge.json"))
    assert report["comparison"]["match"]
    assert report["execution"]["saved_elements"] > 0


def test_budget_enforced_via_service():
    req = _load("peak_budget_chain.json")
    # Stored example passes at 192; shrink the budget to force failure.
    req["peak_budget_elements"] = 100
    req["enforce_budget"] = True
    with pytest.raises(BudgetExceeded):
        handle_request(req)


def test_invalid_service_request():
    with pytest.raises(DAGValidationError):
        handle_request({"inputs": {}, "ops": [], "outputs": []})


def test_bad_seed_type_rejected():
    base = _load("multi_consumer.json")
    for bad in ("abc", 1.5, [1], None):
        with pytest.raises(DAGValidationError, match="'seed'"):
            handle_request({**base, "seed": bad})


def test_oversized_tensors_rejected_before_allocation():
    # Broadcasting two small inputs into a 1e10-element result must be
    # refused during validation, never allocated.
    req = {
        "inputs": {"a": [1, 100000], "b": [100000, 1]},
        "ops": [{"name": "x", "op": "add", "inputs": ["a", "b"]}],
        "outputs": ["x"],
    }
    with pytest.raises(DAGValidationError, match="maximum"):
        handle_request(req)
    req2 = {
        "inputs": {"a": [10_000_000_000]},
        "ops": [{"name": "x", "op": "add", "inputs": ["a", "a"]}],
        "outputs": ["x"],
    }
    with pytest.raises(DAGValidationError, match="maximum"):
        handle_request(req2)


def test_too_many_ops_rejected():
    from tmem.dag import MAX_OPS

    req = {
        "inputs": {"a": [2, 2]},
        "ops": [
            {"name": f"x{i}", "op": "add", "inputs": ["a", "a"]}
            for i in range(MAX_OPS + 1)
        ],
        "outputs": [f"x{MAX_OPS}"],
    }
    with pytest.raises(DAGValidationError, match="too many ops"):
        handle_request(req)


def test_report_is_json_serialisable():
    report = handle_request(_load("shared_view.json"))
    json.dumps(report)  # raises TypeError if numpy/non-serialisable leaks


# ---------------------------------------------------------------------------
# CLI subprocess
# ---------------------------------------------------------------------------

def test_cli_runs_example(tmp_path):
    out_report = tmp_path / "r.json"
    repo = os.path.join(os.path.dirname(__file__), "..")
    env = dict(os.environ, PYTHONPATH=os.path.abspath(os.path.join(repo, "src")))
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "tmem.cli",
            os.path.join(EXAMPLES, "multi_consumer.json"),
            "--save-report",
            str(out_report),
            "--quiet",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "safety=OK" in proc.stdout
    assert "numeric_match=OK" in proc.stdout
    saved = json.loads(out_report.read_text())
    assert saved["status"] == "ok"


def test_cli_main_in_process(capsys, tmp_path):
    from tmem.cli import main

    report_path = tmp_path / "report.json"
    code = main(
        [
            os.path.join(EXAMPLES, "branch_merge.json"),
            "--quiet",
            "--save-report",
            str(report_path),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "safety=OK" in out and "numeric_match=OK" in out
    assert json.loads(report_path.read_text())["status"] == "ok"


def test_cli_missing_file_returns_2():
    from tmem.cli import main

    assert main(["/no/such/request.json"]) == 2


def test_cli_directory_input_returns_2():
    from tmem.cli import main

    assert main([EXAMPLES]) == 2


def test_cli_bad_save_report_path_returns_2(tmp_path):
    from tmem.cli import main

    code = main(
        [
            os.path.join(EXAMPLES, "multi_consumer.json"),
            "--save-report",
            str(tmp_path / "missing" / "r.json"),
        ]
    )
    assert code == 2


def test_cli_non_dict_request_returns_2(tmp_path):
    from tmem.cli import main

    p = tmp_path / "bad.json"
    p.write_text("[1, 2, 3]")
    assert main([str(p), "--enforce-budget"]) == 2


def test_cli_stdin_and_budget_failure():
    repo = os.path.join(os.path.dirname(__file__), "..")
    env = dict(os.environ, PYTHONPATH=os.path.abspath(os.path.join(repo, "src")))
    req = _load("peak_budget_chain.json")
    req["peak_budget_elements"] = 1
    proc = subprocess.run(
        [sys.executable, "-m", "tmem.cli", "-", "--enforce-budget"],
        input=json.dumps(req),
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 2
    assert "budget" in proc.stderr
