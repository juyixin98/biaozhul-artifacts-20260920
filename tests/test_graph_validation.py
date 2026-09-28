"""Tests for graph validation: distinct structural failures must be classified."""
from __future__ import annotations

import pytest


from tenmem.errors import GraphValidationError
from tenmem.fixtures import build_diamond
from tenmem.graph import Graph, Node, validate_and_schedule
from tenmem.tensor import TensorSpec

pytestmark = pytest.mark.unit


def test_diamond_has_expected_waves() -> None:
    schedule = validate_and_schedule(build_diamond(8))
    # Node waves are 0-based: wave 0 r; wave 1 b1,b2 concurrent; wave 2 join.
    assert schedule.waves == (("r",), ("b1", "b2"), ("join",))
    assert schedule.node_wave["join"] == 2


def test_parallel_branches_share_first_wave() -> None:
    from tenmem.fixtures import build_parallel_branches

    schedule = validate_and_schedule(build_parallel_branches(12, 3))
    assert schedule.waves[0] == ("p0", "p1", "p2")


def test_unknown_op_is_input_error() -> None:
    g = Graph(
        "bad",
        (TensorSpec("x", (2,), "float32"),),
        (Node("n", "no_such_op", ("x",), (TensorSpec("y", (2,), "float32"),)),),
        ("y",),
    )
    with pytest.raises(GraphValidationError) as exc:
        validate_and_schedule(g)
    assert exc.value.category == "input_error"
    assert "unknown op" in exc.value.message


def test_arity_mismatch_is_input_error() -> None:
    g = Graph(
        "bad",
        (TensorSpec("x", (2,), "float32"),),
        (Node("n", "add", ("x",), (TensorSpec("y", (2,), "float32"),)),),
        ("y",),
    )
    with pytest.raises(GraphValidationError, match="expects 2 inputs"):
        validate_and_schedule(g)


def test_dangling_tensor_reference() -> None:
    g = Graph(
        "bad",
        (TensorSpec("x", (2,), "float32"),),
        (Node("n", "relu", ("ghost",), (TensorSpec("y", (2,), "float32"),)),),
        ("y",),
    )
    with pytest.raises(GraphValidationError, match="undefined tensor"):
        validate_and_schedule(g)


def test_duplicate_tensor_production() -> None:
    g = Graph(
        "bad",
        (TensorSpec("x", (2,), "float32"),),
        (
            Node("n1", "relu", ("x",), (TensorSpec("y", (2,), "float32"),)),
            Node("n2", "identity", ("x",), (TensorSpec("y", (2,), "float32"),)),
        ),
        ("y",),
    )
    with pytest.raises(GraphValidationError, match="produced more than once"):
        validate_and_schedule(g)


def test_unknown_graph_output() -> None:
    g = Graph(
        "bad",
        (TensorSpec("x", (2,), "float32"),),
        (Node("n", "relu", ("x",), (TensorSpec("y", (2,), "float32"),)),),
        ("zzz",),
    )
    with pytest.raises(GraphValidationError, match="graph output"):
        validate_and_schedule(g)


def test_matmul_contracting_bounds_must_match() -> None:
    g = Graph(
        "mm",
        (
            TensorSpec("a", (2, 3), "float32"),
            TensorSpec("b", (4, 5), "float32"),
        ),
        (Node("m", "matmul", ("a", "b"), (TensorSpec("c", (2, 5), "float32"),)),),
        ("c",),
    )
    with pytest.raises(GraphValidationError, match="contraction bounds differ"):
        validate_and_schedule(g)


def test_nodes_must_be_topologically_ordered() -> None:
    x = TensorSpec("x", (2,), "float32")
    g = Graph(
        "cycle-ish",
        (x,),
        (
            Node("later", "relu", ("early",), (TensorSpec("later", (2,), "float32"),)),
            Node("early", "relu", ("x",), (TensorSpec("early", (2,), "float32"),)),
        ),
        ("later",),
    )
    with pytest.raises(GraphValidationError, match="before its dependencies"):
        validate_and_schedule(g)


def test_alias_requires_own_input_and_output() -> None:
    x = TensorSpec("x", (2,), "float32")
    good = Node("n", "identity", ("x",), (TensorSpec("y", (2,), "float32"),), aliases=(("y", "x"),))
    validate_and_schedule(Graph("ok", (x,), (good,), ("y",)))

    bad = Node("n", "identity", ("x",), (TensorSpec("y", (2,), "float32"),), aliases=(("y", "ghost"),))
    with pytest.raises(GraphValidationError, match="alias"):
        validate_and_schedule(Graph("bad", (x,), (bad,), ("y",)))
