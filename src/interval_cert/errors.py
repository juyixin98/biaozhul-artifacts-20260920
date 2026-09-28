"""Error taxonomy for the certification pipeline.

Every failure carries a stable ``category`` so that API clients and tests can
distinguish input errors, state conflicts, resource exhaustion, domain errors
and unexpected compute failures without parsing message text.
"""

from __future__ import annotations

from typing import Any, Optional


class CertError(Exception):
    """Base class for all expected failures."""

    category = "compute_failure"

    def __init__(self, message: str, *, details: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"category": self.category, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class InputValidationError(CertError):
    """Malformed request: bad expression, bad bounds, bad parameters."""

    category = "input_error"


class ExpressionSyntaxError(InputValidationError):
    """The expression string could not be parsed."""


class UnsupportedExpressionError(InputValidationError):
    """The expression uses constructs outside the supported fragment."""


class StateConflictError(CertError):
    """Parameters are individually valid but mutually inconsistent."""

    category = "state_conflict"


class DomainEvaluationError(CertError):
    """Evaluation left the domain of the expression (log of non-positive,
    division by an interval containing zero, ...).

    The offending location inside the expression tree is preserved so the
    caller can see *where* the evaluation failed.
    """

    category = "domain_error"

    def __init__(
        self,
        message: str,
        *,
        location: str,
        interval: Optional[str] = None,
        reason: Optional[str] = None,
    ):
        details: dict[str, Any] = {"location": location}
        if interval is not None:
            details["interval"] = interval
        if reason is not None:
            details["reason"] = reason
        super().__init__(message, details=details)
        self.location = location


class ResourceExhaustionError(CertError):
    """Step/interval budget was consumed before a verdict was reached."""

    category = "resource_exhausted"


class ComputeFailureError(CertError):
    """Unexpected internal failure."""

    category = "compute_failure"
