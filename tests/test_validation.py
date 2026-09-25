"""输入校验：失败状态与输入范围。"""

from mospp.api import solve_request

from .helpers import req


def base_edges():
    return [(0, 1, 1, 1), (1, 2, 1, 1)]


def test_missing_edges():
    r = {"source": 0, "target": 1}
    assert solve_request(r)["status"] == "invalid_request"


def test_not_an_object():
    assert solve_request([1, 2])["status"] == "invalid_request"


def test_missing_source_target():
    r = {"edges": [{"from": 0, "to": 1, "time": 1, "cost": 1}]}
    assert solve_request(r)["status"] == "invalid_request"


def test_unknown_endpoint():
    r = req(base_edges(), 0, 5)
    assert solve_request(r)["status"] == "invalid_request"


def test_negative_weight_rejected():
    r = req([(0, 1, -1, 1)], 0, 1)
    assert solve_request(r)["status"] == "invalid_request"


def test_nan_inf_weight_rejected():
    for bad in (float("nan"), float("inf"), float("-inf")):
        r = {
            "edges": [{"from": 0, "to": 1, "time": bad, "cost": 1}],
            "source": 0,
            "target": 1,
        }
        assert solve_request(r)["status"] == "invalid_request"


def test_bool_and_string_weights_rejected():
    r = {
        "edges": [{"from": 0, "to": 1, "time": True, "cost": "x"}],
        "source": 0,
        "target": 1,
    }
    assert solve_request(r)["status"] == "invalid_request"


def test_bool_node_id_rejected():
    r = {
        "nodes": [True, 1],
        "edges": [{"from": True, "to": 1, "time": 1, "cost": 1}],
        "source": True,
        "target": 1,
    }
    assert solve_request(r)["status"] == "invalid_request"


def test_duplicate_nodes_rejected():
    r = {
        "nodes": ["a", "a"],
        "edges": [],
        "source": "a",
        "target": "a",
    }
    assert solve_request(r)["status"] == "invalid_request"


def test_edge_references_unknown_node_rejected():
    r = req([(0, 1, 1, 1)], 0, 1, nodes=[0])
    assert solve_request(r)["status"] == "invalid_request"


def test_edge_missing_weight_field():
    r = {
        "edges": [{"from": 0, "to": 1, "time": 1}],
        "source": 0,
        "target": 1,
    }
    assert solve_request(r)["status"] == "invalid_request"


def test_bad_budget():
    r = req(base_edges(), 0, 2, cost_budget=-3)
    assert solve_request(r)["status"] == "invalid_request"


def test_bad_eps():
    r = req(base_edges(), 0, 2, eps=0.5)
    assert solve_request(r)["status"] == "invalid_request"


def test_label_cap_range():
    assert solve_request(req(base_edges(), 0, 2, label_cap=0))["status"] == "limit_exceeded"
    assert solve_request(req(base_edges(), 0, 2, label_cap=10**9))["status"] == "limit_exceeded"
    assert solve_request(req(base_edges(), 0, 2, label_cap=1.5))["status"] == "invalid_request"


def test_weight_over_limit():
    r = req([(0, 1, 1e12, 1)], 0, 1)
    assert solve_request(r)["status"] == "limit_exceeded"


def test_error_response_has_message():
    resp = solve_request(req([(0, 1, -1, 1)], 0, 1))
    assert resp["status"] == "invalid_request"
    assert isinstance(resp["error"], str) and resp["error"]
    assert resp["paths"] == []
