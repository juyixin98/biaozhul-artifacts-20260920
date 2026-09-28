"""Error taxonomy for the root certification service.

Every failure the service can produce maps to one stable ``ErrorCode`` so that
input errors, state conflicts, resource exhaustion and computation failures
are distinguishable programmatically (and in the test suite).

Categories (the ``category`` field is the coarse, testable class):

* ``input``             - the request itself is invalid
* ``state_conflict``    - individually valid parameters contradict each other
* ``domain``            - the expression is not real-valued at some position
* ``resource``          - a configured computational budget was exhausted
* ``computation``       - an unexpected numerical/computational failure
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ErrorCategory(str, Enum):
    INPUT = "input"
    STATE_CONFLICT = "state_conflict"
    DOMAIN = "domain"
    RESOURCE = "resource"
    COMPUTATION = "computation"


class ErrorCode(str, Enum):
    # --- input ---
    PARSE_ERROR = "PARSE_ERROR"
    INVALID_INTERVAL = "INVALID_INTERVAL"
    INVALID_NUMBER = "INVALID_NUMBER"
    REQUEST_VALIDATION = "REQUEST_VALIDATION"
    UNSUPPORTED_EXPRESSION = "UNSUPPORTED_EXPRESSION"
    EXPRESSION_TOO_LARGE = "EXPRESSION_TOO_LARGE"
    # --- state conflict ---
    CONFLICTING_PARAMETERS = "CONFLICTING_PARAMETERS"
    # --- domain ---
    DOMAIN_ERROR = "DOMAIN_ERROR"
    # --- resource ---
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    # --- computation ---
    COMPUTATION_FAILED = "COMPUTATION_FAILED"


# Default HTTP status per category. Resource exhaustion is *not* an HTTP error
# here: the response carries partial, still-useful results and HTTP 200.
HTTP_STATUS: dict[ErrorCategory, int] = {
    ErrorCategory.INPUT: 400,
    ErrorCategory.STATE_CONFLICT: 409,
    ErrorCategory.DOMAIN: 422,
    ErrorCategory.RESOURCE: 200,
    ErrorCategory.COMPUTATION: 500,
}


@dataclass
class SourcePosition:
    """Character span inside the original expression string."""

    start: int
    end: int
    snippet: str

    def as_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "snippet": self.snippet}


@dataclass
class CoreError(Exception):
    """Base class for all service errors with a stable code."""

    code: ErrorCode
    message: str
    category: ErrorCategory
    position: SourcePosition | None = None
    details: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None

    def __str__(self) -> str:
        loc = ""
        if self.position is not None:
            loc = f" at [{self.position.start}:{self.position.end}]"
        return f"{self.code.value}{loc}: {self.message}"

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code.value,
            "category": self.category.value,
            "message": self.message,
            "details": self.details,
        }
        if self.position is not None:
            out["position"] = self.position.as_dict()
        if self.run_id is not None:
            out["run_id"] = self.run_id
        return out


class ParseError(CoreError):
    def __init__(self, message: str, start: int, end: int, snippet: str) -> None:
        super().__init__(
            code=ErrorCode.PARSE_ERROR,
            message=message,
            category=ErrorCategory.INPUT,
            position=SourcePosition(start, end, snippet),
        )


class InvalidRequest(CoreError):
    def __init__(
        self,
        message: str,
        code: ErrorCode = ErrorCode.INVALID_INTERVAL,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code=code,
            message=message,
            category=ErrorCategory.INPUT,
            details=details or {},
        )


class StateConflict(CoreError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            code=ErrorCode.CONFLICTING_PARAMETERS,
            message=message,
            category=ErrorCategory.STATE_CONFLICT,
            details=details or {},
        )


class DomainEvaluationError(CoreError):
    """Raised when a value-range (interval) evaluation leaves the real domain.

    The source position of the offending sub-expression is always attached.
    """

    def __init__(self, message: str, position: SourcePosition) -> None:
        super().__init__(
            code=ErrorCode.DOMAIN_ERROR,
            message=message,
            category=ErrorCategory.DOMAIN,
            position=position,
        )


class ResourceExhausted(CoreError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            code=ErrorCode.RESOURCE_EXHAUSTED,
            message=message,
            category=ErrorCategory.RESOURCE,
            details=details or {},
        )


class ComputationFailure(CoreError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            code=ErrorCode.COMPUTATION_FAILED,
            message=message,
            category=ErrorCategory.COMPUTATION,
            details=details or {},
        )
