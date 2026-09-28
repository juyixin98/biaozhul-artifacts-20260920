"""Unit tests for the tensor type and its input-boundary validation."""

from __future__ import annotations

import numpy as np
import pytest

from app.core.errors import InvalidInputError
from app.core.tensor import (
    SUPPORTED_DTYPE,
    TensorDesc,
    TensorValue,
    coerce_array,
)


@pytest.mark.unit
def test_tensor_desc_numel_is_exact_element_count() -> None:
    desc = TensorDesc(shape=(2, 3, 4), name="t")
    assert desc.numel == 24
    assert desc.nbytes == 24 * 8


@pytest.mark.unit
@pytest.mark.parametrize("shape", [(), (0,), (2, -1), (2.0, 3)])
def test_tensor_desc_rejects_bad_shapes(shape) -> None:
    with pytest.raises(InvalidInputError) as exc:
        TensorDesc(shape=shape)
    assert exc.value.category == "input_error"


@pytest.mark.unit
def test_tensor_value_rejects_non_finite() -> None:
    with pytest.raises(InvalidInputError) as exc:
        TensorValue(np.array([1.0, np.nan]))
    assert exc.value.code == "E_TENSOR_NONFINITE"
    with pytest.raises(InvalidInputError):
        TensorValue(np.array([np.inf]))


@pytest.mark.unit
def test_tensor_value_coerces_to_float64() -> None:
    tv = TensorValue(np.array([1, 2, 3], dtype=np.int32))
    assert tv.data.dtype == SUPPORTED_DTYPE
    assert tv.shape == (3,)


@pytest.mark.unit
def test_coerce_array_rejects_scalar_and_non_finite() -> None:
    with pytest.raises(InvalidInputError) as exc:
        coerce_array(3.14)
    assert exc.value.code == "E_TENSOR_SCALAR"
    with pytest.raises(InvalidInputError):
        coerce_array([[1.0, float("inf")]])


@pytest.mark.unit
def test_coerce_array_accepts_nested_lists() -> None:
    arr = coerce_array([[1, 2], [3, 4]], name="x")
    assert arr.shape == (2, 2)
    assert arr.dtype == SUPPORTED_DTYPE
