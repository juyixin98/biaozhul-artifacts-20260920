"""Failure categories with stable, machine-readable codes.

Every rejection or undetermined result carries one of these codes plus a
human-readable reason and key state, so clients (and tests) can distinguish
*malformed input* from *the method provably cannot finish within budget*.
"""
from __future__ import annotations

from enum import Enum


class FailureCode(str, Enum):
    # --- rejections (4xx): the request itself is invalid ----------------
    INVALID_COEFFICIENTS = "INVALID_COEFFICIENTS"
    EMPTY_POLYNOMIAL = "EMPTY_POLYNOMIAL"
    DEGREE_EXCEEDED = "DEGREE_EXCEEDED"
    COEFFICIENT_TOO_LARGE = "COEFFICIENT_TOO_LARGE"
    TOO_MANY_COEFFICIENTS = "TOO_MANY_COEFFICIENTS"
    INVALID_INTERVAL = "INVALID_INTERVAL"
    INVALID_PRECISION = "INVALID_PRECISION"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"

    # --- undetermined (200 with status=undetermined) --------------------
    BISECTION_BUDGET_EXCEEDED = "BISECTION_BUDGET_EXCEEDED"
    STURM_LENGTH_EXCEEDED = "STURM_LENGTH_EXCEEDED"
    SIGN_UNDETERMINED = "SIGN_UNDETERMINED"

    # --- server-side (5xx) ----------------------------------------------
    INTERNAL_ERROR = "INTERNAL_ERROR"


class RootIsolationError(Exception):
    """Base class for expected kernel/service failures.

    ``state`` holds small, non-sensitive key facts for diagnostics; it must
    never contain raw user payloads (coefficients are redacted upstream).
    """

    code: FailureCode = FailureCode.INTERNAL_ERROR

    def __init__(self, message: str, *, state: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.state = dict(state or {})


class InvalidPolynomialError(RootIsolationError):
    def __init__(self, message: str, *, code: FailureCode, state: dict | None = None) -> None:
        super().__init__(message, state=state)
        self.code = code


class BudgetExceeded(RootIsolationError):
    def __init__(self, message: str, *, code: FailureCode, state: dict | None = None) -> None:
        super().__init__(message, state=state)
        self.code = code


class InvalidRequestError(RootIsolationError):
    """Rejection carrying a specific non-polynomial failure code."""

    def __init__(self, message: str, *, code: FailureCode, state: dict | None = None) -> None:
        super().__init__(message, state=state)
        self.code = code
