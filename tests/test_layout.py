"""Unit tests for the pure layout math."""
from __future__ import annotations

import numpy as np
import pytest

from tensor_backend.tensor import layout
from tensor_backend.tensor.errors import (
    AxisError,
    InvalidLayoutError,
    InvalidStrideError,
    OutOfBoundsError,
    ReshapeCopyRequiredError,
    ShapeOverflowError,
)


def test_c_strides_match_numpy():
    assert layout.c_strides((3, 4)) == (4, 1)
    assert layout.f_strides((3, 4)) == (1, 3)


def test_address_bounds_negative_stride():
    # shape (4,), stride -1, offset 3 -> reaches [0, 3]
    assert layout.address_bounds((4,), (-1,), 3) == (0, 3)
    # shape (2, 2), strides (-2, 1), offset 2 -> rows at 2,0; cols +0..1 -> [0,3]
    assert layout.address_bounds((2, 2), (-2, 1), 2) == (0, 3)


def test_validate_bounds_detects_overflow():
    with pytest.raises(OutOfBoundsError):
        layout.validate_bounds((3, 3), (3, 1), 0, storage_elements=8)


def test_validate_bounds_negative_offset_rejected():
    with pytest.raises(OutOfBoundsError):
        layout.validate_bounds((2,), (1,), -1, storage_elements=4)


def test_validate_bounds_empty_view_is_never_oob():
    # An empty view accesses nothing; it must be valid even over a 0 buffer.
    layout.validate_bounds((0, 3), (3, 1), 0, storage_elements=0)


def test_size_multiplication_overflow_detected_before_access():
    with pytest.raises(ShapeOverflowError):
        layout.normalize_shape((10 ** 20, 10 ** 20), max_elements=10 ** 40)


def test_normalize_shape_rejects_negative_dim():
    with pytest.raises(InvalidLayoutError):
        layout.normalize_shape((2, -1), max_elements=100)


def test_slice_step_zero_rejected():
    with pytest.raises(InvalidStrideError):
        layout.normalize_slice(slice(0, 4, 0), 10)


@pytest.mark.parametrize("index,length,expected", [
    (slice(None), 5, layout.SlicedAxis(0, 1, 5)),
    (slice(None, None, -1), 5, layout.SlicedAxis(4, -1, 5)),
    (slice(1, None, 2), 5, layout.SlicedAxis(1, 2, 2)),
    (slice(None, None, -2), 5, layout.SlicedAxis(4, -2, 3)),
    (-1, 5, (4, 0)),
])
def test_normalize_slice(index, length, expected):
    assert layout.normalize_slice(index, length) == expected


def test_integer_index_oob():
    with pytest.raises(OutOfBoundsError):
        layout.normalize_slice(5, 4)


def test_apply_index_reverse_multi_axis():
    shape, strides = (3, 4), (4, 1)
    ns, nst, off = layout.apply_index(shape, strides, 0, (slice(None, None, -1), slice(None, None, -1)))
    assert ns == (3, 4)
    assert nst == (-4, -1)
    assert off == 2 * 4 + 3  # first-axis start 2 *4, second-axis start 3 *1


def test_apply_index_integer_collapses_axis():
    ns, nst, off = layout.apply_index((3, 4), (4, 1), 0, (2, slice(1, 3)))
    assert ns == (2,)
    assert nst == (1,)
    assert off == 9


def test_too_many_indices():
    with pytest.raises(AxisError):
        layout.apply_index((2,), (1,), 0, (0, 0))


def test_transpose_permutation_validation():
    with pytest.raises(AxisError):
        layout.transpose_layout((2, 3), (3, 1), (0, 0))


def test_broadcast_strides_zero_on_new_axes():
    assert layout.broadcast_strides((3,), (1,), (2, 1, 3)) == (0, 0, 1)
    assert layout.broadcast_strides((1, 3), (5, 1), (2, 3)) == (0, 1)


def test_broadcast_strides_incompatible():
    from tensor_backend.tensor.errors import BroadcastError
    with pytest.raises(BroadcastError):
        layout.broadcast_strides((3,), (1,), (4,))


def test_reshape_element_count_mismatch():
    with pytest.raises(InvalidLayoutError):
        layout.zero_copy_reshape((2, 3), (3, 1), (7,))


def test_reshape_contiguous_is_view():
    assert layout.zero_copy_reshape((2, 3), (3, 1), (6,)) == (1,)
    assert layout.zero_copy_reshape((2, 3), (3, 1), (3, 2)) == (2, 1)


def test_reshape_transposed_flatten_requires_copy():
    # (3,4) transposed has strides (1,4): C-order flattening is non-contiguous.
    with pytest.raises(ReshapeCopyRequiredError):
        layout.zero_copy_reshape((4, 3), (1, 4), (12,))


def test_reshape_strided_slice_split_requires_copy():
    # shape (2,4), strides (2,1) from x[::2]: flatten(8) must copy, but
    # (2,2,2) is a view — matches numpy. Forced strides follow f(k): the new
    # leading axis advances by 2 storage positions (f(4)=2), hence (2,2,1).
    with pytest.raises(ReshapeCopyRequiredError):
        layout.zero_copy_reshape((2, 4), (2, 1), (8,))
    assert layout.zero_copy_reshape((2, 4), (2, 1), (2, 2, 2)) == (2, 2, 1)


def test_reshape_negative_1d_is_view():
    assert layout.zero_copy_reshape((6,), (-1,), (6,)) == (-1,)


def test_is_c_contiguous_skips_size_one():
    assert layout.is_c_contiguous((1, 3, 1), (99, 1, 5))
    assert not layout.is_c_contiguous((3, 4), (1, 3))
