"""Targeted edge/error-path tests to cover defensive branches."""
from __future__ import annotations

import numpy as np
import pytest

from tensor_backend import cli
from tensor_backend.tensor import Tensor, layout, ops
from tensor_backend.tensor.errors import (
    AxisError,
    BroadcastError,
    InvalidLayoutError,
)


def test_is_f_contiguous():
    assert layout.is_f_contiguous((2, 3), (1, 2))
    assert not layout.is_f_contiguous((2, 3), (3, 1))
    assert layout.is_f_contiguous((1, 3), (99, 1))


def test_normalize_axis_0d_and_range():
    with pytest.raises(AxisError):
        layout.normalize_axis(0, 0)
    with pytest.raises(AxisError):
        layout.normalize_axis(3, 2)
    assert layout.normalize_axis(-1, 3) == 2


def test_negative_offset_rejected():
    with pytest.raises(Exception):
        Tensor.from_layout(np.arange(4.0), (2,), (1,), offset=-1)


def test_ndim_mismatch_layout():
    with pytest.raises(InvalidLayoutError):
        Tensor.from_layout(np.arange(4.0), (2, 2), (2,))


def test_boolean_index_rejected(matrix_3x4):
    t, _ = matrix_3x4
    with pytest.raises(InvalidLayoutError):
        _ = t[True]


def test_slice_negative_start_stop(matrix_3x4):
    t, arr = matrix_3x4
    np.testing.assert_array_equal(t[-2:, ::].materialize(), arr[-2:, ::])
    np.testing.assert_array_equal(t[:, :-1].materialize(), arr[:, :-1])


def test_broadcast_to_smaller_shape_rejected():
    with pytest.raises(InvalidLayoutError):
        layout.broadcast_strides((2, 3), (3, 1), (3,))


def test_unary_unknown_op_rejected():
    t = Tensor.from_values([1.0])
    with pytest.raises(KeyError):
        ops.unary("does_not_exist", t)


def test_binary_unknown_op_rejected():
    t = Tensor.from_values([1.0])
    with pytest.raises(KeyError):
        ops.binary("nope", t, t)


def test_reduce_bad_axis(matrix_3x4):
    t, _ = matrix_3x4
    with pytest.raises(AxisError):
        ops.reduce_sum(t, axis=5)


def test_matmul_requires_dimensions():
    s = Tensor.from_values(np.array(1.0))
    with pytest.raises(BroadcastError):
        ops.matmul(s, s)


def test_reshape_two_inferred_dims_rejected(matrix_3x4):
    t, _ = matrix_3x4
    with pytest.raises(InvalidLayoutError):
        t.reshape((-1, -1))


def test_squeeze_errors(matrix_3x4):
    t, _ = matrix_3x4
    with pytest.raises(AxisError):
        t.squeeze(axis=9)
    with pytest.raises(InvalidLayoutError):
        t.squeeze(axis=0)  # axis 0 has size 3


def test_graph_unknown_opcode_rejected():
    from tensor_backend.graph import ComputeGraph
    g = ComputeGraph()
    g.constant("x", [1.0])
    with pytest.raises(Exception):
        g.execute_plan({"steps": [{"op": "bogus", "out": "z", "src": "x"}]})


def test_graph_unknown_handle_rejected():
    from tensor_backend.graph import ComputeGraph
    g = ComputeGraph()
    with pytest.raises(KeyError):
        g.transpose("z", "missing")


def test_cli_validate_exit_zero(capsys):
    rc = cli.main(["validate"])
    assert rc == 0
    assert '"passed"' in capsys.readouterr().out


def test_cli_demo_exit_zero(capsys):
    rc = cli.main(["demo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "trace:" in out and "training losses" in out
