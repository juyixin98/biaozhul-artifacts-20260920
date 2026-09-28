"""Input validation: specific error codes, never generic success."""
from __future__ import annotations

import numpy as np
import pytest

from app.errors import ErrorCode
from app.numerical_input.sparse_matrix import from_coo
from app.numerical_input.fixtures import banded


def test_accepts_symmetric_both_triangles():
    fx = banded(6)
    si = from_coo(fx.n, fx.rows, fx.cols, fx.vals)
    assert si.n == 6
    # upper triangle only, diagonal present
    coo = si.upper.tocoo()
    assert np.all(coo.row <= coo.col)
    assert si.upper.nnz >= 6


def test_rejects_out_of_range_index():
    with pytest.raises(Exception) as ei:
        from_coo(2, [0, 1, 2], [0, 1, 1], [1.0, 1.0, 1.0])
    assert ei.value.code == ErrorCode.INVALID_INDICES


def test_rejects_negative_index():
    with pytest.raises(Exception) as ei:
        from_coo(2, [0, -1, 1], [0, 1, 1], [1.0, 1.0, 1.0])
    assert ei.value.code == ErrorCode.INVALID_INDICES


def test_rejects_duplicate_coordinates():
    # (1,1) supplied twice
    rows = [0, 0, 1, 1]
    cols = [0, 1, 1, 1]
    vals = [2.0, -1.0, -1.0, 2.0]
    with pytest.raises(Exception) as ei:
        from_coo(2, rows, cols, vals)
    assert ei.value.code == ErrorCode.DUPLICATE_ENTRIES


def test_rejects_missing_diagonal():
    # edge (0,1)/(1,0) and diagonal at 0 only; diagonal at 1 missing
    with pytest.raises(Exception) as ei:
        from_coo(2, [0, 0, 1], [0, 1, 0], [2.0, -1.0, -1.0])
    assert ei.value.code == ErrorCode.INVALID_INDICES
    assert ei.value.details["index"] == 1


def test_rejects_asymmetric_pattern():
    rows = [0, 1, 1, 0]
    cols = [0, 1, 0, 1]
    # (0,1) present, (1,0) value differs structurally handled first
    with pytest.raises(Exception) as ei:
        from_coo(3, [0, 1, 2, 0], [0, 1, 2, 2], [2.0, 2.0, 2.0, -1.0])
    assert ei.value.code == ErrorCode.ASYMMETRIC


def test_rejects_asymmetric_values():
    with pytest.raises(Exception) as ei:
        from_coo(2, [0, 0, 1, 1], [0, 1, 0, 1],
                 [2.0, -1.0, -0.5, 2.0])
    assert ei.value.code == ErrorCode.ASYMMETRIC


def test_rejects_non_positive_dimension():
    with pytest.raises(Exception) as ei:
        from_coo(0, [], [], [])
    assert ei.value.code == ErrorCode.INVALID_SHAPE


def test_rejects_nonfinite_values():
    with pytest.raises(Exception) as ei:
        from_coo(2, [0, 0, 1, 1], [0, 1, 0, 1],
                 [2.0, np.nan, -1.0, 2.0])
    assert ei.value.code == ErrorCode.INVALID_SHAPE


def test_rejects_length_mismatch():
    with pytest.raises(Exception) as ei:
        from_coo(2, [0, 1], [0, 1, 0], [1.0, 1.0, 1.0])
    assert ei.value.code == ErrorCode.INVALID_SHAPE
