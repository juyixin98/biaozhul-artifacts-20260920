"""Unit tests for graph construction, validation and wave computation."""

from __future__ import annotations

import pytest

from tensor_mem.errors import GraphValidationError, InputValidationError
from tensor_mem.graph import GraphBuilder


@pytest.mark.unit
def test_unknown_op_is_input_error():
    b = GraphBuilder().feed("x", "float32", (2, 2))
    with pytest.raises(InputValidationError) as exc:
        b.node("n1", "does_not_exist", ["x"], ["y"])
    assert exc.value.category == "input_error"
    assert exc.value.details["op"] == "does_not_exist"


@pytest.mark.unit
def test_arity_mismatch_is_graph_error():
    b = GraphBuilder().feed("x", "float32", (2, 2))
    with pytest.raises(GraphValidationError) as exc:
        b.node("n1", "add", ["x"], ["y"])
    assert exc.value.category == "graph_validation_error"
    assert exc.value.details["expected"] == 2


@pytest.mark.unit
def test_unknown_input_reference():
    b = GraphBuilder().feed("x", "float32", (2, 2))
    with pytest.raises(GraphValidationError) as exc:
        b.node("n1", "relu", ["ghost"], ["y"])
    assert exc.value.details["input"] == "ghost"


@pytest.mark.unit
def test_duplicate_name_rejected():
    b = GraphBuilder().feed("x", "float32", (2, 2))
    b.node("n1", "relu", ["x"], ["t"])
    with pytest.raises(GraphValidationError) as exc:
        b.node("n2", "relu", ["t"], ["t"])  # output reuses name t
    assert exc.value.details["name"] == "t"


@pytest.mark.unit
def test_matmul_inner_dim_mismatch():
    b = (
        GraphBuilder()
        .feed("a", "float32", (2, 3))
        .feed("b", "float32", (4, 5))
    )
    with pytest.raises(GraphValidationError) as exc:
        b.node("n1", "matmul", ["a", "b"], ["c"])
    assert exc.value.details["a_shape"] == "2x3"
    assert exc.value.details["b_shape"] == "4x5"


@pytest.mark.unit
def test_binary_shape_mismatch_no_implicit_broadcast():
    b = (
        GraphBuilder()
        .feed("a", "float32", (2, 2))
        .feed("b", "float32", (2, 3))
    )
    with pytest.raises(GraphValidationError):
        b.node("n1", "add", ["a", "b"], ["c"])


@pytest.mark.unit
def test_dtype_mismatch_rejected():
    b = (
        GraphBuilder()
        .feed("a", "float32", (2, 2))
        .feed("b", "float64", (2, 2))
    )
    with pytest.raises(GraphValidationError):
        b.node("n1", "add", ["a", "b"], ["c"])


@pytest.mark.unit
def test_cycle_detected():
    # The builder forbids forward references, so a cycle can only arise via
    # tampered structure; drive the topological sort directly to prove it
    # reports the cycle rather than silently producing a truncated order.
    from dataclasses import replace

    b = GraphBuilder().feed("x", "float32", (2, 2))
    b.node("n1", "relu", ["x"], ["t"])
    b.node("n2", "relu", ["t"], ["u"])
    b._nodes["n1"] = replace(b._nodes["n1"], inputs=("u",))  # n1 -> n2 -> n1
    b.graph_outputs(["u"])
    with pytest.raises(GraphValidationError) as exc:
        b.build()
    assert set(exc.value.details["nodes"]) == {"n1", "n2"}


@pytest.mark.unit
def test_build_requires_outputs():
    b = GraphBuilder().feed("x", "float32", (2, 2))
    b.node("n1", "relu", ["x"], ["y"])
    with pytest.raises(GraphValidationError):
        b.build()


@pytest.mark.unit
def test_unknown_graph_output():
    b = GraphBuilder().feed("x", "float32", (2, 2))
    b.node("n1", "relu", ["x"], ["y"])
    with pytest.raises(GraphValidationError) as exc:
        b.graph_outputs(["missing"])  # validated eagerly at declaration
    assert exc.value.details["output"] == "missing"


@pytest.mark.unit
def test_diamond_waves_group_parallel_branches():
    from tensor_mem.fixtures import build_diamond_graph

    g = build_diamond_graph()
    assert g.waves == [
        ["n_relu"],
        ["n_add_branch", "n_mul_branch"],
        ["n_join"],
    ]
    assert g.order == ["n_relu", "n_add_branch", "n_mul_branch", "n_join"]


@pytest.mark.unit
def test_reshape_preserves_numel_contract():
    b = GraphBuilder().feed("x", "float32", (2, 6))
    with pytest.raises(GraphValidationError):
        b.node("n1", "reshape", ["x"], ["v"], {"shape": [3, 5]})


@pytest.mark.unit
def test_transpose_perm_validation():
    b = GraphBuilder().feed("x", "float32", (2, 3))
    with pytest.raises(GraphValidationError):
        b.node("n1", "transpose", ["x"], ["v"], {"perm": [0, 0]})


@pytest.mark.unit
def test_inferred_metadata_matches_expected_shapes():
    from tensor_mem.fixtures import build_workspace_graph

    g = build_workspace_graph()
    n_mm1 = g.node("n_mm1")
    assert n_mm1.out_meta[0].shape.as_tuple() == (4, 2)
    n_mm2 = g.node("n_mm2")
    assert n_mm2.out_meta[0].shape.as_tuple() == (4, 3)
