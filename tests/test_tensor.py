"""Unit tests for tensor primitives: shapes, dtypes, aligned sizing."""

from __future__ import annotations

import numpy as np
import pytest

from tensor_mem.errors import InputValidationError
from tensor_mem.tensor import (
    DEFAULT_ALIGNMENT,
    Shape,
    TensorMeta,
    TensorType,
    align_up,
    aligned_byte_size,
    byte_size,
    shape_from,
)


@pytest.mark.unit
def test_shape_validates_positive_dims():
    with pytest.raises(InputValidationError) as exc:
        Shape(2, 0)
    assert exc.value.category == "input_error"
    assert exc.value.details["dims"] == [2, 0]


@pytest.mark.unit
def test_shape_rejects_negative_and_non_int():
    with pytest.raises(InputValidationError):
        Shape(-1, 2)
    with pytest.raises(InputValidationError):
        Shape(2.0, 2)  # type: ignore[arg-type]


@pytest.mark.unit
def test_shape_numel_and_str():
    s = Shape(2, 3, 4)
    assert s.numel == 24
    assert s.rank == 3
    assert str(s) == "2x3x4"
    assert s.with_dim(1, 7).as_tuple() == (2, 7, 4)


@pytest.mark.unit
def test_tensor_type_rejects_unknown_dtype():
    with pytest.raises(InputValidationError) as exc:
        TensorType("bfloat16", 2)
    assert exc.value.category == "input_error"
    assert exc.value.details["dtype"] == "bfloat16"
    assert set(exc.value.details["supported"]) == {
        "float32", "float64", "int32", "int64"
    }


@pytest.mark.unit
def test_byte_size_and_alignment_count_padding():
    s = Shape(3, 3)  # 9 float32 = 36 bytes
    assert byte_size(s, "float32") == 36
    assert aligned_byte_size(s, "float32", DEFAULT_ALIGNMENT) == 64
    assert aligned_byte_size(Shape(4, 4), "float32", 64) == 64
    assert align_up(1, 64) == 64
    assert align_up(64, 64) == 64
    assert align_up(65, 64) == 128


@pytest.mark.unit
def test_align_up_rejects_non_positive_alignment():
    with pytest.raises(InputValidationError):
        align_up(10, 0)


@pytest.mark.unit
def test_tensor_meta_rank_mismatch_is_input_error():
    with pytest.raises(InputValidationError) as exc:
        TensorMeta(TensorType("float32", 3), Shape(2, 2))
    assert exc.value.details["type_rank"] == 3
    assert exc.value.details["shape_rank"] == 2


@pytest.mark.unit
def test_shape_from_coercion_rules():
    assert shape_from((2, 3)).as_tuple() == (2, 3)
    assert shape_from([2, 3]).as_tuple() == (2, 3)
    assert shape_from(Shape(2, 3)).as_tuple() == (2, 3)
    with pytest.raises(InputValidationError):
        shape_from([])
    with pytest.raises(InputValidationError):
        shape_from("2x3")  # type: ignore[arg-type]


@pytest.mark.unit
def test_numpy_dtype_mapping():
    assert TensorType("float32", 1).numpy_dtype == np.dtype(np.float32)
    assert TensorType("int64", 1).itemsize == 8
    meta = TensorMeta(TensorType("int32", 2), Shape(2, 2))
    assert meta.bytes() == 16
    assert meta.dtype == "int32"
