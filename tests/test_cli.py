"""CLI 端到端测试（solve / demo / list-scenarios）。"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from pose_graph_optimizer.cli import build_parser, main


def test_parser_solve_requires_request():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["solve"])


def test_cli_solve_json_request_file(tmp_path):
    # 用库构造请求并落盘，再走 CLI solve 全流程
    from pose_graph_optimizer.synthetic import standard_scenarios

    graph, _ = standard_scenarios(seed=42)["square"]["builder"]()
    payload = {
        "name": "cli-square",
        "nodes": [
            {"id": n.id, "pose": list(n.initial_pose), "fixed": n.fixed}
            for n in graph.nodes.values()
        ],
        "edges": [
            {
                "i": e.i,
                "j": e.j,
                "measurement": list(e.measurement),
                "info": e.info.tolist(),
                "kernel": {"type": e.kernel.name, "delta": e.kernel.delta},
            }
            for e in graph.edges
        ],
        "options": {"max_iterations": 30},
    }
    req = tmp_path / "req.json"
    out = tmp_path / "resp.json"
    req.write_text(json.dumps(payload), encoding="utf-8")

    rc = main(["solve", "-r", str(req), "-o", str(out)])
    assert rc == 0
    response = json.loads(out.read_text(encoding="utf-8"))
    assert response["success"] is True
    assert response["summary"]["final_cost"] < response["summary"]["initial_cost"]


def test_cli_solve_stderr_reports_cost(capsys, tmp_path):
    from pose_graph_optimizer.synthetic import standard_scenarios

    graph, _ = standard_scenarios(seed=42)["line_no_loop"]["builder"]()
    payload = {
        "nodes": [
            {"id": n.id, "pose": list(n.initial_pose), "fixed": n.fixed}
            for n in graph.nodes.values()
        ],
        "edges": [
            {
                "i": e.i,
                "j": e.j,
                "measurement": list(e.measurement),
                "info": e.info.tolist(),
            }
            for e in graph.edges
        ],
    }
    req = tmp_path / "req.json"
    req.write_text(json.dumps(payload), encoding="utf-8")
    rc = main(["solve", "-r", str(req)])
    captured = capsys.readouterr()
    assert rc in (0, 3)
    assert "代价" in captured.err


def test_cli_demo_scenario_to_file(tmp_path):
    out = tmp_path / "demo.json"
    rc = main(["demo", "-s", "circle_wrap", "-o", str(out)])
    assert rc == 0
    response = json.loads(out.read_text(encoding="utf-8"))
    assert response["success"] is True
    assert "ground_truth_max_error" in response


def test_cli_demo_disconnected_returns_error_code(tmp_path):
    out = tmp_path / "broken.json"
    rc = main(["demo", "-s", "disconnected", "-o", str(out)])
    assert rc == 1
    response = json.loads(out.read_text(encoding="utf-8"))
    assert response["success"] is False
    assert len(response["diagnostics"]["components"]) == 2


def test_cli_unknown_scenario_exit_2(capsys):
    rc = main(["demo", "-s", "nope"])
    assert rc == 2


def test_cli_list_scenarios_prints_all(capsys):
    rc = main(["list-scenarios"])
    captured = capsys.readouterr()
    assert rc == 0
    for name in ("square", "circle_wrap", "disconnected", "line_no_loop"):
        assert name in captured.out


def test_cli_bad_json_exit_2(tmp_path):
    req = tmp_path / "bad.json"
    req.write_text("{bad", encoding="utf-8")
    rc = main(["solve", "-r", str(req)])
    assert rc == 2


def test_cli_solve_disconnected_exit_1_and_writes_response(tmp_path):
    from pose_graph_optimizer.synthetic import standard_scenarios

    graph, _ = standard_scenarios(seed=42)["disconnected"]["builder"]()
    payload = {
        "nodes": [
            {"id": n.id, "pose": list(n.initial_pose), "fixed": n.fixed}
            for n in graph.nodes.values()
        ],
        "edges": [
            {
                "i": e.i,
                "j": e.j,
                "measurement": list(e.measurement),
                "info": e.info.tolist(),
            }
            for e in graph.edges
        ],
    }
    req = tmp_path / "broken.json"
    out = tmp_path / "resp.json"
    req.write_text(json.dumps(payload), encoding="utf-8")
    rc = main(["solve", "-r", str(req), "-o", str(out)])
    assert rc == 1
    response = json.loads(out.read_text(encoding="utf-8"))
    assert response["success"] is False
    assert len(response["diagnostics"]["components"]) == 2


def test_module_invocation_works(tmp_path):
    # python -m pose_graph_optimizer
    proc = subprocess.run(
        [sys.executable, "-m", "pose_graph_optimizer", "list-scenarios"],
        cwd=__import__("pathlib").Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0
    assert "square" in proc.stdout
