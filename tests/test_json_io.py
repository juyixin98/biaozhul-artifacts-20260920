"""End-to-end tests for the JSON request/response interface and CLI."""

import json
import subprocess
import sys

import numpy as np

from pose_graph.json_io import run_from_dict
from pose_graph.synthetic import build_square_graph


def _square_request_dict(**kwargs):
    graph, _ = build_square_graph(**kwargs)
    return {
        "options": {"max_iterations": 50},
        "nodes": [
            {
                "id": node.index,
                "pose": list(node.initial_pose),
                "fixed": node.index in graph.fixed_nodes,
            }
            for node in graph.nodes
        ],
        "edges": [
            {
                "i": e.i,
                "j": e.j,
                "z": list(e.z),
                "information": e.information.tolist(),
                "kernel": {
                    "type": e.kernel_type,
                    **(
                        {"parameter": e.kernel_parameter}
                        if e.kernel_parameter is not None
                        else {}
                    ),
                },
                "label": e.label,
            }
            for e in graph.edges
        ],
    }


def test_json_happy_path(tmp_path):
    request = _square_request_dict()
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))

    proc = subprocess.run(
        [sys.executable, "-m", "pose_graph.cli", str(request_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    response = json.loads(proc.stdout)
    assert response["status"] == "ok"
    summary = response["summary"]
    assert summary["cost_final"] < summary["cost_initial"] * 0.01
    assert summary["converged"] is True
    assert len(response["poses"]) == 20
    assert "global optimality" in response["note"]
    assert len(response["cost_history"]) == summary["iterations"] + 1


def test_json_output_to_file(tmp_path):
    response = run_from_dict(_square_request_dict())
    out = tmp_path / "out.json"
    from pose_graph.json_io import dump_response

    dump_response(response, out)
    reloaded = json.loads(out.read_text())
    assert reloaded["status"] == "ok"


def test_json_robust_kernel_roundtrip(tmp_path):
    request = _square_request_dict(
        bad_loop_closure=True,
        bad_kernel_type="huber",
        bad_kernel_parameter=1.0,
    )
    response = run_from_dict(request)
    assert response["status"] == "ok"
    labels = {s["label"] for s in response["edge_stats"]}
    assert "bad_loop_closure" in labels


def test_json_missing_nodes_returns_structured_error():
    response = run_from_dict({"edges": []})
    assert response["status"] == "error"
    assert response["error"]["type"] == "GraphStructureError"
    assert "nodes" in response["error"]["message"]


def test_json_edge_unknown_node_rejected():
    response = run_from_dict(
        {
            "nodes": [{"id": 0, "pose": [0, 0, 0], "fixed": True}],
            "edges": [
                {
                    "i": 0,
                    "j": 5,
                    "z": [1, 0, 0],
                    "information": np.eye(3).tolist(),
                }
            ],
        }
    )
    assert response["status"] == "error"


def test_json_disconnected_report_and_cli_exit_code(tmp_path):
    request = _square_request_dict()
    # Detach the last node by removing all edges touching it.
    request["edges"] = [
        e for e in request["edges"] if e["i"] != 19 and e["j"] != 19
    ]
    response = run_from_dict(request)
    assert response["status"] == "ok"
    assert response["connectivity"]["is_connected"] is False
    assert 19 in response["summary"]["auto_anchored_nodes"]

    path = tmp_path / "bad.json"
    path.write_text("{not valid json")
    proc = subprocess.run(
        [sys.executable, "-m", "pose_graph.cli", str(path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2


def test_unknown_option_rejected():
    request = _square_request_dict()
    request["options"] = {"bogus": 1}
    response = run_from_dict(request)
    assert response["status"] == "error"
    assert "unknown option" in response["error"]["message"]
