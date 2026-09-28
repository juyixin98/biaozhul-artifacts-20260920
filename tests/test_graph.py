"""计算图构建与校验测试：具体输入错误与失败类别。"""

from __future__ import annotations

import pytest

from recomp_scheduler.errors import GraphValidationError
from recomp_scheduler.graph import build_graph


def test_linear_chain_structure(chain_graph) -> None:
    assert chain_graph.order == ["x", "l1", "r1", "d1", "l2"]
    assert chain_graph.inputs == ("x",)
    assert chain_graph.outputs == ("l2",)
    # 形状推断是具体值，不只是"能调用"。
    assert chain_graph.node("l1").shape == (4, 3)
    assert chain_graph.node("l2").shape == (4, 1)


def test_branch_graph_detects_shared_subgraph(branch_graph) -> None:
    # d1 被 a1 与 b1 两个消费者使用——共享子图识别。
    assert branch_graph.is_shared("d1")
    assert branch_graph.shared_nodes() == ["d1"]
    assert branch_graph.consumers["d1"] == ["a1", "b1"]
    # 普通节点不被误判。
    assert not branch_graph.is_shared("l1")


def test_unknown_op_is_input_error() -> None:
    with pytest.raises(GraphValidationError) as exc:
        build_graph([{"id": "z", "op": "does-not-exist"}])
    assert exc.value.category == "input_error"
    assert exc.value.details["op"] == "does-not-exist"
    assert "linear" in exc.value.details["known"]


def test_edge_to_later_node_is_input_error() -> None:
    # raw 列表必须是拓扑序：前向引用尚未定义的节点要在构建期拒绝。
    raw = [
        {"id": "y", "op": "relu", "inputs": ["x"]},
        {"id": "x", "op": "input", "attrs": {"shape": [2]}},
    ]
    with pytest.raises(GraphValidationError) as exc:
        build_graph(raw, outputs=["y"])
    assert exc.value.category == "input_error"
    assert exc.value.details["edge"] == "x"


def test_wrong_arity_is_input_error() -> None:
    raw = [
        {"id": "x", "op": "input", "attrs": {"shape": [2, 2]}},
        {"id": "a", "op": "add", "inputs": ["x"]},  # add 需要两个输入
    ]
    with pytest.raises(GraphValidationError) as exc:
        build_graph(raw, outputs=["a"])
    assert exc.value.category == "input_error"
    assert "expects 2 inputs" in exc.value.message


def test_shape_mismatch_on_add_is_input_error() -> None:
    raw = [
        {"id": "x", "op": "input", "attrs": {"shape": [2, 2]}},
        {"id": "z", "op": "input", "attrs": {"shape": [2, 3]}},
        {"id": "a", "op": "add", "inputs": ["x", "z"]},
    ]
    with pytest.raises(GraphValidationError) as exc:
        build_graph(raw, outputs=["a"])
    assert exc.value.category == "input_error"
    assert exc.value.details["shapes"] == [(2, 2), (2, 3)]


def test_dead_node_rejected() -> None:
    # 一个不指向任何输出的孤立节点必须被拒绝（不可达死节点）。
    raw = [
        {"id": "x", "op": "input", "attrs": {"shape": [2]}},
        {"id": "a", "op": "relu", "inputs": ["x"]},
        {"id": "orphan", "op": "relu", "inputs": ["x"]},
    ]
    with pytest.raises(GraphValidationError) as exc:
        build_graph(raw, outputs=["a"])
    assert exc.value.category == "input_error"
    assert exc.value.details["dead_nodes"] == ["orphan"]


def test_duplicate_node_id_rejected() -> None:
    raw = [
        {"id": "x", "op": "input", "attrs": {"shape": [2]}},
        {"id": "x", "op": "relu", "inputs": ["x"]},
    ]
    with pytest.raises(GraphValidationError):
        build_graph(raw, outputs=["x"])


def test_dropout_keep_prob_bounds(chain_graph) -> None:
    # keep_prob=0 在执行期是输入类别错误（属性非法）。
    raw = [
        {"id": "x", "op": "input", "attrs": {"shape": [2]}},
        {"id": "d", "op": "dropout", "inputs": ["x"], "attrs": {"keep_prob": 0.0}},
    ]
    g = build_graph(raw, outputs=["d"])
    from recomp_scheduler.state import TrainState
    from recomp_scheduler.planner import make_plan
    from recomp_scheduler.engine import execute

    state = TrainState.synthetic(g)
    with pytest.raises(GraphValidationError):
        execute(state, make_plan((), len(g.order)))


def test_missing_required_attr_is_input_error() -> None:
    raw = [
        {"id": "x", "op": "input", "attrs": {"shape": [2, 2]}},
        {"id": "lin", "op": "linear", "inputs": ["x"], "attrs": {}},  # 缺 out_features
    ]
    with pytest.raises(GraphValidationError) as exc:
        build_graph(raw, outputs=["lin"])
    assert exc.value.category == "input_error"


def test_input_node_with_edges_rejected() -> None:
    with pytest.raises(GraphValidationError):
        build_graph(
            [{"id": "x", "op": "input", "inputs": ["y"], "attrs": {"shape": [2]}}]
        )


def test_compute_node_without_inputs_rejected() -> None:
    with pytest.raises(GraphValidationError):
        build_graph([{"id": "r", "op": "relu", "inputs": []}])


def test_bad_node_id_type_rejected() -> None:
    with pytest.raises(GraphValidationError):
        build_graph([{"id": 5, "op": "input", "attrs": {"shape": [2]}}])


def test_non_dict_attrs_rejected() -> None:
    with pytest.raises(GraphValidationError):
        build_graph(
            [
                {"id": "x", "op": "input", "attrs": {"shape": [2]}},
                {"id": "r", "op": "relu", "inputs": ["x"], "attrs": [1, 2]},
            ]
        )


def test_declared_unknown_output_rejected() -> None:
    with pytest.raises(GraphValidationError):
        build_graph(
            [{"id": "x", "op": "input", "attrs": {"shape": [2]}}],
            outputs=["ghost"],
        )


def test_node_is_not_object_rejected() -> None:
    with pytest.raises(GraphValidationError):
        build_graph(["not-a-dict"])  # type: ignore[list-item]


def test_empty_graph_rejected() -> None:
    with pytest.raises(GraphValidationError):
        build_graph([])


def test_non_string_input_reference_rejected() -> None:
    with pytest.raises(GraphValidationError):
        build_graph(
            [
                {"id": "x", "op": "input", "attrs": {"shape": [2]}},
                {"id": "r", "op": "relu", "inputs": [1]},
            ]
        )
