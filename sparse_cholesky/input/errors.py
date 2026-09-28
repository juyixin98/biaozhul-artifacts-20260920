"""Typed errors for the sparse Cholesky backend.

Failures are categorized explicitly rather than surfaced as generic runtime
errors, so the service layer can report a concrete ``error_type`` and tests
can assert on failure classes.
"""
from __future__ import annotations


class SparseCholeskyError(Exception):
    """Base class for all backend errors."""

    #: Stable machine-readable category used in API responses.
    error_type = "sparse_cholesky_error"


# ---- Input validation -------------------------------------------------------


class InputValidationError(SparseCholeskyError):
    error_type = "input_validation_error"


class MatrixShapeError(InputValidationError):
    error_type = "matrix_shape_error"


class OffDiagonalLowerError(InputValidationError):
    error_type = "off_diagonal_lower_error"


class DuplicateEntryError(InputValidationError):
    error_type = "duplicate_entry_error"


class MatrixNotFiniteError(InputValidationError):
    error_type = "matrix_not_finite_error"


class MatrixNotSymmetricError(InputValidationError):
    error_type = "matrix_not_symmetric_error"


# ---- Numeric failures -------------------------------------------------------


class FactorizationError(SparseCholeskyError):
    """Base class for failures discovered while factoring."""

    error_type = "factorization_error"

    def __init__(self, message: str, *, pivot_index: int | None = None,
                 pivot_value: float | None = None,
                 ordering_index: int | None = None) -> None:
        super().__init__(message)
        #: Matrix index (in the *permuted* ordering) where the failure occurs.
        self.pivot_index = pivot_index
        #: Value of the offending pivot.
        self.pivot_value = pivot_value
        #: Original (pre-permutation) index of the offending pivot, if known.
        self.ordering_index = ordering_index


class NonPositiveDefiniteError(FactorizationError):
    """A zero or negative pivot proves the matrix is not positive definite."""

    error_type = "non_positive_definite_error"


class PivotTooSmallError(FactorizationError):
    """A positive but numerically unreliable pivot (below configured tol)."""

    error_type = "pivot_too_small_error"


# ---- Symbolic cache ---------------------------------------------------------


class SymbolicStructureMismatchError(SparseCholeskyError):
    """Reuse requested with a matrix whose sparsity pattern differs."""

    error_type = "symbolic_structure_mismatch_error"
