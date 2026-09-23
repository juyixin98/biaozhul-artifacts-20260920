"""Typed error states for the sparse-CG library.

Every failure the library can report maps to one of these exceptions.
Solver *outcomes* (non-convergence, non-positive curvature, divergence,
stagnation) are not exceptions: :func:`solve_pcg` returns a CGResult with
an explicit status string so callers always get diagnostics.

Hierarchy
---------
SparseCgError
  +-- ValidationError                      (abstract base for input errors)
        +-- InvalidCsrError                (malformed CSR arrays)
        +-- NonSymmetricMatrixError        (A != A^T within tolerance)
        +-- NotPositiveDefiniteError       (diagonal <= 0 detected up front)
        +-- InvalidRequestError            (bad JSON request payload)
"""

from __future__ import annotations


class SparseCgError(Exception):
    """Base class for every sparse_cg error."""


class ValidationError(SparseCgError):
    """Base class for errors caused by invalid caller input.

    ``code`` is a stable machine-readable identifier surfaced in JSON API
    error responses (``"error.code"``); it is part of the public contract.
    """

    code: str = "validation_error"


class InvalidCsrError(ValidationError):
    """CSR arrays fail structural validation (shape, pointers, indices)."""

    code = "invalid_csr"


class NonSymmetricMatrixError(ValidationError):
    """Matrix is not symmetric within the requested tolerance.

    Attributes
    ----------
    max_abs_diff:
        Largest |A_ij - A_ji| found when comparing matching sparsity
        patterns.  ``-1.0`` signals a pattern mismatch (an (i,j) entry
        whose mirrored (j,i) entry does not exist).
    position:
        ``(row, col)`` at which the offending pair was found.
    """

    code = "non_symmetric_matrix"

    def __init__(self, message: str, *, max_abs_diff: float = -1.0,
                 position: tuple[int, int] | None = None) -> None:
        super().__init__(message)
        self.max_abs_diff = max_abs_diff
        self.position = position


class NotPositiveDefiniteError(ValidationError):
    """Non-positive diagonal found up front; the matrix cannot be SPD.

    Note that a positive diagonal is necessary but not sufficient.  Loss of
    positive-definiteness that only shows up inside the iteration (negative
    curvature) is reported via ``CGResult(status="non_positive_curvature")``.
    """

    code = "not_positive_definite"

    def __init__(self, message: str, *, index: int = -1,
                 diagonal_value: float | None = None) -> None:
        super().__init__(message)
        self.index = index
        self.diagonal_value = diagonal_value


class InvalidRequestError(ValidationError):
    """JSON request payload is malformed or contains out-of-range values."""

    code = "invalid_request"
