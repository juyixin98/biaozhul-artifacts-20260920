"""Tests covering JSON validation branches and in-process CLI entry point."""

import json

import numpy as np
import pytest

from pose_graph.cli import main as cli_main
from pose_graph.json_io import (
    dump_response,
    load_request,
    run_from_dict,
)
from pose_graph.graph import GraphStructureError
from pose_graph.optimizer import OptimizeOptions, optimize
from pose_graph.synthetic import build_square_graph


def _valid_payload():
    graph, _ = build_square_graph()
    return {
        "nodes": [
            {"id": n.index, "pose": list(n.initial_pose),
             "fixed": n.index in graph.fixed_nodes}
            for n in graph.nodes
        ],
        "edges": [
            {"i": e.i, "j": e.j, "z": list(e.z),
             "information": e.information.tolist()}
            for e in graph.edges
        ],
    }


def test_load_and_dump_roundtrip(tmp_path):
    payload = _valid_payload()
    path = tmp_path / "req.json"
    path.write_text(json.dumps(payload))
    assert load_request(path)["nodes"][0]["id"] == 0
    response = run_from_dict(load_request(path))
    out = tmp_path / "resp.json"
    dump_response(response, out)
    assert json.loads(out.read_text())["status"] == "ok"


def test_cli_main_success_and_file_output(tmp_path, capsys):
    payload = _valid_payload()
    req = tmp_path / "req.json"
    req.write_text(json.dumps(payload))
    out = tmp_path / "resp.json"
    rc = cli_main([str(req), "-o", str(out)])
    assert rc == 0
    assert json.loads(out.read_text())["status"] == "ok"

    rc_stdout = cli_main([str(req)])
    assert rc_stdout == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_cli_main_missing_file(tmp_path):
    rc = cli_main([str(tmp_path / "nope.json")])
    assert rc == 2


def test_cli_main_invalid_json(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json")
    assert cli_main([str(path)]) == 2


def test_cli_main_structure_error_exit_code(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"nodes": []}))
    assert cli_main([str(path)]) == 2


@pytest.mark.parametrize(
    "payload, fragment",
    [
        ([1, 2], "JSON object"),
        ({"nodes": []}, "non-empty list"),
        ({"nodes": [{"id": 0}]}, "'pose'"),
        ({"nodes": [{"id": 0, "pose": [0, 0]}]}, "3 numbers"),
        ({"nodes": [{"id": 0, "pose": [0, 0, 0]}], "edges": {}}, "must be a list"),
        (
            {"nodes": [{"id": 0, "pose": [0, 0, 0], "fixed": True}],
             "edges": [{"i": 0, "j": 1}]},
            "missing field",
        ),
        (
            {"nodes": [{"id": 0, "pose": [0, 0, 0], "fixed": True},
                       {"id": 1, "pose": [1, 0, 0]}],
             "edges": [{"i": 0, "j": 1, "z": [1, 0],
                        "information": np.eye(3).tolist()}]},
            "'z'",
        ),
        (
            {"nodes": [{"id": 0, "pose": [0, 0, 0], "fixed": True},
                       {"id": 1, "pose": [1, 0, 0]}],
             "edges": [{"i": 0, "j": 1, "z": [1, 0, 0],
                        "information": [[1, 0], [0, 1]]}]},
            "3x3",
        ),
        (
            {"nodes": [{"id": 0, "pose": [0, 0, 0], "fixed": True},
                       {"id": 1, "pose": [1, 0, 0]}],
             "edges": [{"i": 0, "j": 1, "z": [1, 0, 0],
                        "information": np.eye(3).tolist(),
                        "kernel": {"notype": "x"}}]},
            "kernel",
        ),
    ],
)
def test_build_graph_validation_errors(payload, fragment):
    response = run_from_dict(payload)
    assert response["status"] == "error"
    assert fragment in response["error"]["message"]


def test_options_must_be_object():
    payload = _valid_payload()
    payload["options"] = [1, 2]
    response = run_from_dict(payload)
    assert response["status"] == "error"
    assert "options" in response["error"]["message"]


def test_options_bad_value_type():
    payload = _valid_payload()
    payload["options"] = {"max_iterations": "lots"}
    response = run_from_dict(payload)
    assert response["status"] == "error"
    assert "max_iterations" in response["error"]["message"]


def test_unknown_kernel_surfaces_from_optimizer():
    graph, _ = build_square_graph()
    graph.add_edge(
        0, 5, [0.1, 0.1, 0.0], np.eye(3), kernel_type="bogus_kernel"
    )
    with pytest.raises(GraphStructureError):
        optimize(graph, OptimizeOptions(max_iterations=5))


def test_termination_max_iterations():
    graph, _ = build_square_graph()
    result = optimize(graph, OptimizeOptions(max_iterations=1))
    assert result.iterations == 1
    assert result.termination_reason == "max_iterations_reached"
    assert result.converged is False


def test_termination_on_large_xtol():
    graph, _ = build_square_graph()
    result = optimize(graph, OptimizeOptions(xtol=1.0e6))
    # The very first accepted step is immediately below the huge xtol.
    assert result.termination_reason == "step_size_below_xtol"
    assert result.converged is True


def test_reoptimizing_converged_graph_terminates_on_gradient():
    # Noise-free graph initialized EXACTLY at ground truth: zero cost, zero
    # gradient, so the solver stops before taking any step.
    from pose_graph.graph import PoseGraph
    from pose_graph.se2 import relative_pose
    from pose_graph.synthetic import square_ground_truth

    gt = square_ground_truth(per_side=4)
    graph = PoseGraph()
    info = np.diag([20.0, 20.0, 25.0])
    for k, pose in enumerate(gt):
        graph.add_node(k, pose, fixed=(k == 0))
    for k in range(len(gt) - 1):
        graph.add_edge(k, k + 1, relative_pose(gt[k], gt[k + 1]), info)
    graph.add_edge(len(gt) - 1, 0, relative_pose(gt[-1], gt[0]), info)

    result = optimize(graph, OptimizeOptions())
    assert result.iterations == 0
    assert result.cost_final < 1e-20
    assert result.termination_reason == "gradient_below_gtol"
    assert result.converged is True
