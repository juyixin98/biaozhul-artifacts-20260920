"""Error taxonomy shared across layers.

Failures are classified explicitly so clients (and tests) can branch on a
stable category instead of parsing human-readable messages. Uncertain
outcomes - a decomposition exists but its evidence failed a gate - are a
separate category from hard failures.
"""

from enum import StrEnum
from typing import Any


class ErrorCategory(StrEnum):
    """Stable machine-readable failure categories."""

    INVALID_MATRIX = "INVALID_MATRIX"
    """Empty, non-square, ragged, non-finite (NaN/Inf) or non-numeric input."""

    INVALID_PARAMETER = "INVALID_PARAMETER"
    """A malformed request option/parameter (not the matrix contents)."""

    NON_SYMMETRIC = "NON_SYMMETRIC"
    """``|A - A^T|`` exceeds the requested relative/absolute tolerance."""

    SIZE_LIMIT_EXCEEDED = "SIZE_LIMIT_EXCEEDED"
    """Dimension exceeds the configured ``max_n`` size budget."""

    VALUE_OUT_OF_RANGE = "VALUE_OUT_OF_RANGE"
    """A finite entry is too large in magnitude for safe float64 arithmetic
    on the whole matrix; the matrix must be rescaled before decomposition."""

    NON_CONVERGENCE = "NON_CONVERGENCE"
    """The QR iteration exhausted its sweep budget before deflation.

    Crucially this is never reported as success merely because the iteration
    loop stopped.
    """

    UNCERTAIN_RESULT = "UNCERTAIN_RESULT"
    """A result was produced, but a residual/orthogonality evidence gate
    failed, or an independent cross-check is indeterminate at the float64
    conditioning limit. The eigenvalues/vectors are returned together with
    the explicit uncertainties instead of a success verdict."""


# HTTP status used for each category. Uncertain results are delivered as HTTP
# 200 (a body exists); hard request failures use 422.
HTTP_STATUS: dict[ErrorCategory, int] = {
    ErrorCategory.INVALID_MATRIX: 422,
    ErrorCategory.INVALID_PARAMETER: 422,
    ErrorCategory.NON_SYMMETRIC: 422,
    ErrorCategory.SIZE_LIMIT_EXCEEDED: 422,
    ErrorCategory.VALUE_OUT_OF_RANGE: 422,
    ErrorCategory.NON_CONVERGENCE: 422,
    ErrorCategory.UNCERTAIN_RESULT: 200,
}


class EigServiceError(Exception):
    """A classified, expected service failure (never a 500 bug)."""

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.details: dict[str, Any] = dict(details or {})

    @property
    def http_status(self) -> int:
        return HTTP_STATUS[self.category]
