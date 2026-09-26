"""JSON 入口：解析、校验、端到端求解与错误诊断响应。"""

from __future__ import annotations

import json

import numpy as np
import pytest

from pose_graph_optimizer.io_json import (
    ParseError,
    graph_from_dict,
    graph_error_to_dict,
    run_request_dict,
)
from pose_graph_optimizer.synthetic import standard_scenarios


def _square_payload() -> dict:
    graph, _ = standard_scenarios(seed=42)["square"]["builder"]()
    nodes = [
        {"id": n.id, "pose": list(n.initial_pose), "fixed": n.fixed}
        for n in graph.nodes.values()
    ]
    edges = [
        {
            "id": e.id,
            "i": e.i,
            "j": e.j,
            "measurement": list(e.measurement),
            "info": e.info.tolist(),
            "kernel": {"type": e.kernel.name, "delta": e.kernel.delta},
        }
        for e in graph.edges
    ]
    return {"name": "square-json", "nodes": nodes, "edges": edges}


def test_roundtrip_request_solves_and_cost_drops():
    payload = _square_payload()
    graph, options = graph_from_dict(payload)
    assert len(graph.nodes) == 25
    assert graph.fixed_node_count() == 1
    from pose_graph_optimizer.optimizer import optimize

    result = optimize(graph, options)
    assert result.final_cost < result.initial_cost
    assert result.success


def test_run_request_dict_full_response_structure():
    response = run_request_dict(_square_payload())
    assert response["success"] is True
    assert response["summary"]["final_cost"] < response["summary"]["initial_cost"]
    assert len(response["poses"]) == 25
    assert response["poses"][0]["fixed"] is True
    # 错误回环被 Tukey 归零
    false = next(e for e in response["edges"] if e["i"] == 3 and e["j"] == 15)
    assert false["robust_weight"] == pytest.approx(0.0, abs=1e-6)
    # JSON 可序列化
    json.dumps(response, ensure_ascii=False)
    assert "不承诺全局最优" in response["diagnostics"]["note"]


def test_info_as_sigma_object_expands_to_matrix():
    payload = {
        "nodes": [
            {"id": 0, "pose": [0, 0, 0], "fixed": True},
            {"id": 1, "pose": [1, 0, 0]},
        ],
        "edges": [
            {
                "i": 0,
                "j": 1,
                "measurement": [1, 0, 0],
                "info": {"sigma_xy": 0.1, "sigma_theta": 0.05},
            }
        ],
    }
    graph, _ = graph_from_dict(payload)
    assert np.isclose(graph.edges[0].info[0, 0], 100.0)
    response = run_request_dict(payload)
    assert response["success"] is True


def test_disconnected_request_returns_diagnostic_not_raise():
    graph, _ = standard_scenarios(seed=42)["disconnected"]["builder"]()
    payload = {
        "name": "broken",
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
    response = run_request_dict(payload)
    assert response["success"] is False
    assert response["summary"]["status"] == "graph_error"
    assert "不连通" in response["summary"]["message"]
    assert len(response["diagnostics"]["components"]) == 2


@pytest.mark.parametrize(
    "patch,match",
    [
        (lambda p: p["nodes"].append({"id": 0, "pose": [0, 0, 0]}), "重复"),
        (lambda p: p.update({"edges": [{"i": 0, "j": 99}]}), "measurement"),
        (lambda p: p.update({"nodes": []}), "不能为空"),
        (
            lambda p: p["edges"][0].update(
                {"info": [[1, 0, 0], [0, -1, 0], [0, 0, 1]]}
            ),
            "正定",
        ),
        (
            lambda p: p["edges"][0].update(
                {"info": [[1, 0, 0], [0.5, 1, 0], [0, 0, 1]]}
            ),
            "对称",
        ),
        (lambda p: p["edges"][0].update({"kernel": {"type": "l1"}}), "鲁棒核"),
        (lambda p: p["nodes"][0].update({"pose": [0, 0]}), "长度为 3"),
        (lambda p: p.update({"nodes": "x"}), "必须是数组"),
        (lambda p: p.update({"nodes": [{"id": "a", "pose": [0, 0, 0]}]}), "整数"),
        (lambda p: p.update({"options": {"max_iterations": -3}}), "options"),
        (lambda p: p.update({"options": {"tol_step": "x"}}), "类型非法"),
        (lambda p: p["edges"][0].update({"id": "x"}), "整数"),
        (lambda p: p["nodes"][0].update({"pose": [1, 0, float("nan")]}), "有限"),
        (
            lambda p: p["edges"][0].update(
                {"info": {"sigma_xy": -0.1, "sigma_theta": 0.05}}
            ),
            "正定|非法",
        ),
    ],
)
def test_invalid_payloads_rejected(patch, match):
    payload = _square_payload()
    patch(payload)
    with pytest.raises(ParseError, match=match):
        graph_from_dict(payload)


def test_missing_file_and_bad_json(tmp_path):
    from pose_graph_optimizer.io_json import load_request

    with pytest.raises(ParseError, match="不存在"):
        load_request(str(tmp_path / "nope.json"))
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ParseError, match="JSON 解析失败"):
        load_request(str(bad))


def test_graph_error_dict_is_json_serializable():
    resp = graph_error_to_dict(None, ["图为空：没有节点"])
    json.dumps(resp, ensure_ascii=False)
    assert resp["success"] is False
