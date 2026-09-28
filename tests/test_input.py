"""Tests for input validation and failure categorization."""
from __future__ import annotations

import numpy as np
import pytest

from sparse_cholesky.input.errors import (
    DuplicateEntryError,
    MatrixNotFiniteError,
    MatrixShapeError,
    OffDiagonalLowerError,
)
from sparse_cholesky.input.matrix import build_sparse_matrix


def test_accepts_lower_triangle_and_mirrors_symmetrically():
    n = 3
    m = build_sparse_matrix(n, [0, 1, 2, 1], [0, 1, 2, 0], [4.0, 4.0, 4.0, -1.0])
    dense = m.csc.toarray()
    # Upper triangle must be the mirror of the supplied lower triangle.
    assert dense[0, 1] == -1.0
    assert dense[1, 0] == -1.0
    assert np.allclose(np.diag(dense), 4.0)


def test_rejects_above_diagonal_entry():
    with pytest.raises(OffDiagonalLowerError) as exc:
        build_sparse_matrix(2, [0, 1], [1, 1], [1.0, 2.0])
    assert exc.value.error_type == "off_diagonal_lower_error"


def test_rejects_out_of_range_index():
    with pytest.raises(MatrixShapeError):
        build_sparse_matrix(2, [2], [0], [1.0])


def test_rejects_negative_index():
    with pytest.raises(MatrixShapeError):
        build_sparse_matrix(2, [-1], [0], [1.0])


def test_rejects_mismatched_array_lengths():
    with pytest.raises(MatrixShapeError):
        build_sparse_matrix(3, [0, 1], [0], [1.0, 2.0])


def test_rejects_nonpositive_order():
    with pytest.raises(MatrixShapeError):
        build_sparse_matrix(0, [], [], [])


def test_rejects_duplicate_entry():
    with pytest.raises(DuplicateEntryError) as exc:
        build_sparse_matrix(3, [1, 1], [0, 0], [2.0, 3.0])
    assert exc.value.error_type == "duplicate_entry_error"


def test_rejects_nonfinite_value():
    with pytest.raises(MatrixNotFiniteError):
        build_sparse_matrix(2, [0, 1, 1], [0, 1, 0], [1.0, 1.0, np.nan])


def test_rejects_explicit_zero_off_diagonal():
    with pytest.raises(MatrixShapeError):
        build_sparse_matrix(2, [0, 1, 1], [0, 1, 0], [1.0, 1.0, 0.0])


def test_keeps_zero_diagonal_as_structural_slot():
    # A zero diagonal is valid input but must fail numerically as non-PD.
    m = build_sparse_matrix(2, [0, 1, 1], [0, 1, 0], [0.0, 2.0, -1.0])
    assert m.csc[0, 0] == 0.0
    # The explicit zero diagonal is not stored; only the 3 genuine nonzeros
    # remain (off-diagonal -1 mirrored + diagonal 2).
    assert m.nnz_symmetric == 3
    from sparse_cholesky.core import FactorizationEngine
    from config.settings import FactorizationConfig
    from sparse_cholesky.input.errors import NonPositiveDefiniteError
    with pytest.raises(NonPositiveDefiniteError) as exc:
        FactorizationEngine(FactorizationConfig()).factor(m)
    assert exc.value.pivot_index == 0


def test_rejects_missing_diagonal_slot():
    # Row 0 has no (0, 0) entry at all -> malformed, not merely non-PD.
    with pytest.raises(MatrixShapeError) as exc:
        build_sparse_matrix(2, [1, 1], [1, 0], [2.0, -1.0])
    assert "diagonal" in str(exc.value)


def test_does_not_densify_large_pattern():
    # Identity of size where a dense n*n float64 buffer would be ~800 MiB.
    n = 10_000
    m = build_sparse_matrix(n, np.arange(n), np.arange(n), np.ones(n))
    assert m.csc.nnz == n
    assert m.density() < 1e-3
