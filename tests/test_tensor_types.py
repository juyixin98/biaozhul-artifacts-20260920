"""Boundary checks for tensor specs."""

import numpy as np
import pytest

from bucket_sync.tensor_types import SUPPORTED_DTYPES, TensorSpec, TensorSpecError

pytestmark = pytest.mark.unit


def test_valid_spec_holds_shape_size_and_dtype():
    spec = TensorSpec("w", (2, 3), "float64")
    assert spec.size == 6
    assert spec.np_dtype == np.dtype("float64")
    spec.validate_value(np.zeros((2, 3)))


@pytest.mark.parametrize(
    "name,shape,dtype",
    [
        ("", (2,), "float64"),
        ("  ", (2,), "float64"),
        ("x", (), "float64"),
        ("x", (0,), "float64"),
        ("x", (-1,), "float64"),
        ("x", (2.0,), "float64"),  # type: ignore[arg-type]
        ("x", (2,), "int32"),
    ],
)
def test_invalid_specs_are_rejected_at_construction(name, shape, dtype):
    with pytest.raises(TensorSpecError):
        TensorSpec(name, shape, dtype)


def test_supported_dtypes_actually_validate():
    for dtype in SUPPORTED_DTYPES:
        TensorSpec("x", (1,), dtype).validate_value(np.zeros(1, dtype=dtype))


def test_shape_mismatch_is_rejected_not_broadcast():
    spec = TensorSpec("w", (2, 2))
    with pytest.raises(TensorSpecError, match="shape mismatch"):
        spec.validate_value(np.zeros((2, 3)))
    with pytest.raises(TensorSpecError, match="shape mismatch"):
        spec.validate_value(np.zeros((4,)))


def test_dtype_mismatch_is_rejected_not_silently_casted():
    spec = TensorSpec("w", (2,), "float64")
    with pytest.raises(TensorSpecError, match="dtype mismatch"):
        spec.validate_value(np.zeros(2, dtype=np.float32))


def test_non_finite_values_are_rejected():
    spec = TensorSpec("w", (3,))
    for bad in (np.array([np.nan, 0.0, 0.0]), np.array([np.inf, 0.0, 0.0])):
        with pytest.raises(TensorSpecError, match="NaN or Inf"):
            spec.validate_value(bad)


def test_specs_are_frozen():
    spec = TensorSpec("w", (1,))
    with pytest.raises(Exception):
        spec.name = "other"  # type: ignore[misc]


def test_zeros_placeholder_has_correct_shape_and_dtype():
    spec = TensorSpec("b", (1,), "float64")
    z = spec.zeros()
    assert z.shape == (1,)
    assert z.dtype == np.float64
    assert z[0] == 0.0
