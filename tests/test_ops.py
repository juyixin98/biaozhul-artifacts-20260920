"""Direct op-contract and kernel tests (including workspace-bearing ops)."""

from __future__ import annotations

import numpy as np
import pytest

from tensor_mem.errors import GraphValidationError, InputValidationError
from tensor_mem.graph import GraphBuilder
from tensor_mem.ops import get_op, op_workspace_bytes
from tensor_mem.tensor import Shape, TensorType


@pytest.mark.unit
def test_registry_shape_and_aliases():
    assert get_op("reshape").alias == (0,)
    assert get_op("transpose").alias == (0,)
    assert get_op("relu").alias == (None,)
    assert get_op("matmul").shape_sensitive is True
    assert get_op("relu").shape_sensitive is False


@pytest.mark.unit
def test_get_unknown_op_is_input_error():
    with pytest.raises(InputValidationError) as exc:
        get_op("frobnicate")
    assert set(exc.value.details["supported"]) >= {"relu", "matmul"}


@pytest.mark.unit
def test_reduce_sum_axis_contract_and_runtime_result():
    b = GraphBuilder().feed("x", "float32", (2, 3))
    b.node("n1", "reduce_sum", ["x"], ["s"], {"axis": 0})
    b.graph_outputs(["s"])
    g = b.build()
    assert g.node("n1").out_meta[0].shape.as_tuple() == (3,)

    from tensor_mem.executor import Executor
    from tensor_mem.fixtures import seeded_input

    x = seeded_input((2, 3), "float32", 5)
    ex = Executor(g)
    handles, report = ex.execute({"x": x}, run_id="reduce")
    np.testing.assert_allclose(handles["s"].array, x.sum(axis=0), rtol=1e-6)
    # reduce workspace (aligned input size = 64B) present; single wave holds
    # input 64 + output 64 + scratch 64 = 192
    assert ex.plan.records["ws::n1"].bytes_required == 64
    assert report.wave_resident[0]["pool_bytes"] == 192
    handles["s"].release()


@pytest.mark.unit
def test_reduce_sum_axis_out_of_range():
    b = GraphBuilder().feed("x", "float32", (2, 3))
    with pytest.raises(GraphValidationError):
        b.node("n1", "reduce_sum", ["x"], ["s"], {"axis": 2})


@pytest.mark.unit
def test_reduce_sum_negative_axis_and_keepdims_kernel():
    b = GraphBuilder().feed("x", "float32", (2, 3))
    b.node("n1", "reduce_sum", ["x"], ["s"], {"axis": -1, "keepdims": True})
    b.graph_outputs(["s"])
    g = b.build()
    assert g.node("n1").out_meta[0].shape.as_tuple() == (2, 1)
    from tensor_mem.executor import Executor

    x = np.arange(6, dtype=np.float32).reshape(2, 3)
    ex = Executor(g)
    handles, _ = ex.execute({"x": x}, run_id="reduce-neg")
    np.testing.assert_allclose(handles["s"].array, x.sum(axis=-1, keepdims=True))
    handles["s"].release()


@pytest.mark.unit
def test_transpose_default_and_explicit_perm_shapes():
    b = GraphBuilder().feed("x", "float32", (2, 3, 4))
    b.node("d", "transpose", ["x"], ["d"])
    b.node("e", "transpose", ["d"], ["e"], {"perm": [1, 0, 2]})
    b.graph_outputs(["e"])
    g = b.build()
    assert g.node("d").out_meta[0].shape.as_tuple() == (4, 3, 2)
    assert g.node("e").out_meta[0].shape.as_tuple() == (3, 4, 2)


@pytest.mark.unit
def test_matmul_rejects_rank_other_than_two():
    b = GraphBuilder().feed("a", "float32", (2, 3, 4)).feed("c", "float32", (4, 5))
    with pytest.raises(GraphValidationError):
        b.node("n1", "matmul", ["a", "c"], ["o"])


@pytest.mark.unit
def test_relu_rejects_integer_operand():
    b = GraphBuilder().feed("x", "int32", (2, 2))
    with pytest.raises(GraphValidationError):
        b.node("n1", "relu", ["x"], ["y"])


@pytest.mark.unit
def test_attribute_type_validation():
    b = GraphBuilder().feed("x", "float32", (2, 6))
    with pytest.raises(InputValidationError):
        b.node("n1", "reshape", ["x"], ["v"], {"shape": [3, "2"]})
    b2 = GraphBuilder().feed("y", "float32", (2, 3))
    with pytest.raises(GraphValidationError):  # 6 elems cannot reshape to 12
        b2.node("n3", "reshape", ["y"], ["v2"], {"shape": [12]})
    b3 = GraphBuilder().feed("z", "float32", (2, 3))
    with pytest.raises(InputValidationError):
        b3.node("n4", "reduce_sum", ["z"], ["s"], {"axis": 0.5})


@pytest.mark.unit
def test_workspace_sized_from_concrete_shapes():
    t = TensorType("float32", 2)
    ws = op_workspace_bytes(
        get_op("matmul"),
        [t, t], [Shape(8, 16), Shape(16, 4)], {}, 64,
    )
    # packed panel: 16 * 4 * 4 = 256 bytes, already aligned
    assert ws == 256
