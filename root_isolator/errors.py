"""Typed error categories shared by the input, kernel, evidence and service layers."""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCategory(str, Enum):
    """Coarse, stable machine-readable failure classes.

    Every reject/indeterminate response carries one of these, plus a
    human-readable message and a redactable ``state`` snapshot.
    """

    MALFORMED_COEFFICIENTS = "MALFORMED_COEFFICIENTS"
    NON_EXACT_COEFFICIENT = "NON_EXACT_COEFFICIENT"
    EMPTY_COEFFICIENTS = "EMPTY_COEFFICIENTS"
    DEGREE_EXCEEDED = "DEGREE_EXCEEDED"
    COEFFICIENT_TOO_LARGE = "COEFFICIENT_TOO_LARGE"
    TOO_MANY_ROOTS = "TOO_MANY_ROOTS"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    INCONCLUSIVE = "INCONCLUSIVE"
    EVIDENCE_MISMATCH = "EVIDENCE_MISMATCH"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# HTTP status selected per category. Categories that describe a malformed or
# inadmissible request are 422; a computation that is correct but cannot be
# finished/decided is 409; genuine faults are 500.
HTTP_STATUS_BY_CATEGORY: dict[ErrorCategory, int] = {
    ErrorCategory.MALFORMED_COEFFICIENTS: 422,
    ErrorCategory.NON_EXACT_COEFFICIENT: 422,
    ErrorCategory.EMPTY_COEFFICIENTS: 422,
    ErrorCategory.DEGREE_EXCEEDED: 422,
    ErrorCategory.COEFFICIENT_TOO_LARGE: 422,
    ErrorCategory.TOO_MANY_ROOTS: 422,
    ErrorCategory.BUDGET_EXCEEDED: 422,
    ErrorCategory.INCONCLUSIVE: 409,
    ErrorCategory.EVIDENCE_MISMATCH: 409,
    ErrorCategory.INTERNAL_ERROR: 500,
}


class IsolationError(Exception):
    """Raised for any classified failure so the service can render a verdict."""

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        state: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.state = dict(state or {})
        self.request_id = request_id

    def with_request_id(self, request_id: str) -> "IsolationError":
        self.request_id = request_id
        return self

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.category.value}: {self.message}"
