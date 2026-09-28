"""Tests for tensor views, ops and the overlap write policy."""
from __future__ import annotations

import numpy as np
import pytest

from tensor_backend.tensor import Tensor, ops
from tensor_backend.tensor.errors import (
    AxisError,
    BroadcastError,
    DTypeError,
    OutOfBoundsError,
    OverlappingWriteError,
    ReshapeCopyRequiredError,
)


def test_transpose_aliases_and_matches_numpy(matrix_3x4):
    t, arr = matrix_3x4
    tr = t.T
    assert tr.shares_storage(t)
    assert tr.strides == (1, 4)
    np.testing.assert_array_equal(tr.materialize(), arr.T)


def test_transpose_writes_alias_source(matrix_3x4):
    t, arr = matrix_3x4
    tr = t.T
    tr.write_fill(7.0)
    # Transpose shares storage: the whole base is overwritten.
    assert np.all(t.materialize() == 7.0)


def test_slice_stepped_and_negative(matrix_3x4):
    t, arr = matrix_3x4
    np.testing.assert_array_equal(t[0:3:2, 1:4].materialize(), arr[0:3:2, 1:4])
    np.testing.assert_array_equal(t[::-1].materialize(), arr[::-1])
    np.testing.assert_array_equal(t[:, ::-2].materialize(), arr[:, ::-2])


def test_integer_index_collapses_axis(matrix_3x4):
    t, arr = matrix_3x4
    col = t[2]
    assert col.shape == (4,)
    assert col.offset == 8
    np.testing.assert_array_equal(col.materialize(), arr[2])


def test_index_out_of_bounds_category(matrix_3x4):
    t, _ = matrix_3x4
    with pytest.raises(OutOfBoundsError):
        _ = t[5]


def test_reshape_view_and_copy(matrix_3x4):
    t, arr = matrix_3x4
    flat = t.reshape((12,))
    assert flat.shares_storage(t)
    with pytest.raises(ReshapeCopyRequiredError):
        t.T.reshape((12,))
    copied = t.T.reshape((12,), allow_copy=True)
    assert not copied.shares_storage(t)
    np.testing.assert_array_equal(copied.materialize(), arr.T.reshape(-1))


def test_reshape_inferred_dimension(matrix_3x4):
    t, _ = matrix_3x4
    r = t.reshape((-1, 6))
    assert r.shape == (2, 6)


def test_unsqueeze_inserts_zero_stride(matrix_3x4):
    t, _ = matrix_3x4
    v = t.unsqueeze(0)
    assert v.shape == (1, 3, 4)
    assert v.strides[0] == 0


def test_self_overlap_detection():
    view = Tensor.from_layout(np.arange(3.0), shape=(3, 3), strides=(0, 1))
    overlaps, uncertain = view.self_overlap()
    assert overlaps is True and uncertain is False
    plain = Tensor.from_values(np.zeros((3, 3)))
    assert plain.self_overlap() == (False, False)


def test_writing_into_self_overlapping_view_rejected():
    view = Tensor.from_layout(np.arange(3.0), shape=(3, 3), strides=(0, 1))
    with pytest.raises(OverlappingWriteError):
        ops.assign(view, Tensor.zeros((3, 3)))


def test_overlapping_assign_raise_vs_temp():
    t = Tensor.from_values(np.arange(6.0))
    dst, src = t[0:4], t[2:6]
    with pytest.raises(OverlappingWriteError):
        ops.assign(dst, src)
    t2 = Tensor.from_values(np.arange(6.0))
    ops.assign(t2[0:4], t2[2:6], overlap_policy=ops.OverlapPolicy.TEMP)
    np.testing.assert_array_equal(t2.materialize()[:4], [2, 3, 4, 5])


def test_inplace_same_layout_update_is_allowed():
    t = Tensor.from_values(np.arange(4.0))
    ops.binary("multiply", t, t, out=t)
    np.testing.assert_array_equal(t.materialize(), [0, 1, 4, 9])


def test_binary_broadcast_matches_numpy():
    a = Tensor.from_values(np.array([[1.0, 2.0, 3.0]]))
    b = Tensor.from_values(np.zeros((4, 3)))
    got = ops.binary("add", a, b)
    np.testing.assert_array_equal(got.materialize(), np.ones((4, 3)) * np.array([1, 2, 3]))


def test_binary_broadcast_failure_category():
    a = Tensor.from_values(np.zeros((3,)))
    b = Tensor.from_values(np.zeros((4,)))
    with pytest.raises(BroadcastError):
        ops.binary("add", a, b)


def test_unary_ops_values():
    t = Tensor.from_values(np.array([-1.0, -2.0, 3.0]))
    np.testing.assert_allclose(ops.unary("abs", t).materialize(), [1, 2, 3])
    np.testing.assert_allclose(ops.unary("negate", t).materialize(), [1, 2, -3])


def test_reduce_and_matmul():
    t = Tensor.from_values(np.arange(6.0).reshape(2, 3))
    np.testing.assert_allclose(ops.reduce_sum(t).materialize().reshape(()), 15.0)
    np.testing.assert_allclose(
        ops.reduce_sum(t, axis=0).materialize(), [3, 5, 7]
    )
    a = Tensor.from_values(np.eye(2))
    b = Tensor.from_values(np.array([[3.0], [4.0]]))
    np.testing.assert_allclose(ops.matmul(a, b).materialize(), [[3], [4]])


def test_empty_tensor_ops():
    e = Tensor.from_values(np.zeros((0, 3)))
    assert ops.unary("square", e).size == 0
    r = e.reshape((0,))
    assert r.shares_storage(e)


def test_scalar_0d_keeps_rank_through_ops():
    s = Tensor.from_values(np.array(3.5))
    assert s.shape == ()
    u = ops.unary("square", s)
    assert u.shape == ()
    np.testing.assert_array_equal(u.materialize(), np.array(12.25))
    matrix = Tensor.from_values(np.arange(6.0).reshape(2, 3))
    scaled = ops.binary("multiply", matrix, s)
    np.testing.assert_allclose(scaled.materialize(), np.arange(6).reshape(2, 3) * 3.5)
    assert ops.reduce_sum(s).shape == ()


def test_storage_is_independent_of_input():
    arr = np.arange(4.0)
    t = Tensor.from_values(arr)
    t.write_fill(99.0)
    # Mutating owned storage must not touch the caller's array.
    np.testing.assert_array_equal(arr, np.arange(4.0))


def test_only_float64_supported():
    with pytest.raises(DTypeError):
        from tensor_backend.tensor.storage import Storage
        Storage(np.zeros(3, dtype=np.int32))


def test_bad_axis_category(cube_2x3x4):
    t, _ = cube_2x3x4
    with pytest.raises(AxisError):
        t.transpose((0, 1))
