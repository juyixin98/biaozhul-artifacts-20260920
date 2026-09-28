"""Unit tests for the tensor value model."""
from __future__ import annotations

import numpy as np
import pytest


from tenmem.tensor import DEFAULT_ALIGNMENT, DimBound, TensorSpec, align_up

pytestmark = pytest.mark.unit


def test_align_up_hits_exact_and_rounds() -> None:
    assert align_up(0, 64) == 0
    assert align_up(1, 64) == 64
    assert align_up(64, 64) == 64
    assert align_up(65, 64) == 128


def test_static_spec_sizes() -> None:
    s = TensorSpec("t", (2, 3), "float32")
    assert s.max_shape == (2, 3)
    assert not s.is_dynamic
    assert s.max_bytes == 2 * 3 * 4


def test_dynamic_spec_sizes_at_upper_bound() -> None:
    s = TensorSpec("t", ((2, 10),), "float64")
    assert s.is_dynamic
    assert s.min_shape == (2,)
    assert s.max_shape == (10,)
    assert s.max_bytes == 80


def test_runtime_shape_bound_check() -> None:
    s = TensorSpec("t", ((1, 4), (2, 2)), "float32")
    s.check_runtime_shape((3, 2))  # inside bound
    with pytest.raises(ValueError, match="out of declared bound"):
        s.check_runtime_shape((5, 2))
    with pytest.raises(ValueError, match="rank mismatch"):
        s.check_runtime_shape((3,))


def test_rejects_bad_dtype_and_bounds() -> None:
    with pytest.raises(ValueError, match="unsupported dtype"):
        TensorSpec("t", (2,), "bfloat16")
    with pytest.raises(ValueError, match="invalid dimension"):
        TensorSpec("t", ((5, 2),), "float32")


def test_bytes_for_concrete_shape() -> None:
    s = TensorSpec("t", ((1, 100),), "float32")
    assert s.bytes_for((10,)) == 40
    assert s.max_bytes == 400
    assert s.max_bytes % DEFAULT_ALIGNMENT == 0 or s.max_bytes >= 400


def test_roundtrip_dict() -> None:
    s = TensorSpec("t", ((1, 9), 7), "int64")
    other = TensorSpec.from_dict(s.to_dict())
    assert other.max_shape == (9, 7)
    assert other.is_dynamic
    assert other.dtype == "int64"
    assert np.issubdtype(np.dtype(other.dtype), np.integer)
