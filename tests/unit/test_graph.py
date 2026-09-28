"""Unit tests for graph validation, topology and reference counting."""

from __future__ import annotations

import pytest

from app.core.errors import InvalidInputError
from app.core.graph import Node, build_graph


def _expect_error(nodes, target, code: str):
    with pytest.raises(InvalidInputError) as exc:
        build_graph(nodes, [target])
    assert exc.value.code == code
    assert exc.value.category == "input_error"
    return exc


@pytest.mark.unit
def test_duplicate_node_id_rejected() -> None:
    nodes = [
        Node("x", "input", (), {"shape": [1, 1]}),
        Node("x", "input", (), {"shape": [1, 1]}),
    ]
    _expect_error(nodes, "x", "E_GRAPH_DUP_ID")


@pytest.mark.unit
def test_missing_reference_rejected() -> None:
    nodes = [
        Node("x", "input", (), {"shape": [1, 2]}),
        Node("W", "parameter", (), {"shape": [2, 2]}),
        Node("h", "linear", ("x", "ghost")),
    ]
    _expect_error(nodes, "h", "E_GRAPH_MISSING_REF")


@pytest.mark.unit
def test_cycle_rejected_with_node_list() -> None:
    nodes = [
        Node("x", "input", (), {"shape": [1, 2]}),
        Node("a", "relu", ("b",)),
        Node("b", "relu", ("a",)),
    ]
    exc = _expect_error(nodes, "b", "E_GRAPH_CYCLE")
    assert set(exc.value.context["nodes_in_cycle"]) == {"a", "b"}


@pytest.mark.unit
def test_shape_mismatch_rejected() -> None:
    nodes = [
        Node("x", "input", (), {"shape": [1, 2]}),
        Node("W", "parameter", (), {"shape": [3, 4]}),  # 2 != 3
        Node("h", "linear", ("x", "W")),
    ]
    _expect_error(nodes, "h", "E_SHAPE_MISMATCH")


@pytest.mark.unit
def test_arity_violation_rejected() -> None:
    nodes = [
        Node("x", "input", (), {"shape": [1, 2]}),
        Node("a", "relu", ("x",)),
        Node("b", "relu", ("a", "x")),  # relu takes exactly one input
    ]
    _expect_error(nodes, "b", "E_OP_ARITY")


@pytest.mark.unit
def test_target_must_exist_and_be_final() -> None:
    nodes = [
        Node("x", "input", (), {"shape": [1, 2]}),
        Node("a", "relu", ("x",)),
        Node("b", "relu", ("a",)),
    ]
    _expect_error(nodes, "ghost", "E_GRAPH_MISSING_TARGET")
    _expect_error(nodes, "a", "E_GRAPH_TARGET_NOT_FINAL")


@pytest.mark.unit
def test_reference_counts_detect_shared_subgraph(branching) -> None:
    g = branching.graph()
    # `shared` feeds both ext and branchB; W3 feeds both branches.
    assert g.user_count["shared"] == 2
    assert g.user_count["W3"] == 2
    assert g.is_shared("shared")
    assert g.is_shared("W3")
    # A single-consumer node is not shared.
    assert g.user_count["h1"] == 1
    assert not g.is_shared("h1")


@pytest.mark.unit
def test_topological_order_respects_edges_and_is_deterministic(branching) -> None:
    g = branching.graph()
    rank = {nid: i for i, nid in enumerate(g.order)}
    for nid, node in g.nodes.items():
        for ref in node.inputs:
            assert rank[ref] < rank[nid]
    # Rebuilding yields the identical order (ties broken deterministically).
    assert branching.graph().order == g.order
