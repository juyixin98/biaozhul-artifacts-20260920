"""Error taxonomy for the HVP service.

Every failure raised by the service is a ``ServiceError`` carrying a stable
``ErrorCategory`` so that callers (and tests) can distinguish:

- input validation errors (client sent something malformed)
- not-found errors (unknown graph id)
- state conflicts (stale parameter version, missing stored point)
- resource exhaustion (node / time budgets)
- computation failures (overflow, domain errors -> non-finite values)
- non-smooth point rejections (kink hit under the "reject" policy)
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCategory(str, Enum):
    INPUT_VALIDATION = "input_validation"
    NOT_FOUND = "not_found"
    STATE_CONFLICT = "state_conflict"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    COMPUTATION_FAILURE = "computation_failure"
    NONSMOOTH_POINT = "nonsmooth_point"
    INTERNAL = "internal"


_HTTP_STATUS: dict[ErrorCategory, int] = {
    ErrorCategory.INPUT_VALIDATION: 400,
    ErrorCategory.NOT_FOUND: 404,
    ErrorCategory.STATE_CONFLICT: 409,
    ErrorCategory.RESOURCE_EXHAUSTED: 429,
    ErrorCategory.COMPUTATION_FAILURE: 422,
    ErrorCategory.NONSMOOTH_POINT: 422,
    ErrorCategory.INTERNAL: 500,
}


class ServiceError(Exception):
    """Base error for all expected service failures."""

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.details: dict[str, Any] = details or {}
        self.run_id = run_id

    @property
    def http_status(self) -> int:
        return _HTTP_STATUS[self.category]

    def to_payload(self) -> dict[str, Any]:
        return {
            "error": {
                "category": self.category.value,
                "message": self.message,
                "details": self.details,
                "run_id": self.run_id,
            }
        }


def input_error(message: str, **details: Any) -> ServiceError:
    return ServiceError(ErrorCategory.INPUT_VALIDATION, message, details=details)


def not_found(message: str, **details: Any) -> ServiceError:
    return ServiceError(ErrorCategory.NOT_FOUND, message, details=details)


def state_conflict(message: str, **details: Any) -> ServiceError:
    return ServiceError(ErrorCategory.STATE_CONFLICT, message, details=details)


def resource_exhausted(message: str, *, run_id: str | None = None, **details: Any) -> ServiceError:
    return ServiceError(ErrorCategory.RESOURCE_EXHAUSTED, message, details=details, run_id=run_id)


def computation_failure(message: str, *, run_id: str | None = None, **details: Any) -> ServiceError:
    return ServiceError(ErrorCategory.COMPUTATION_FAILURE, message, details=details, run_id=run_id)


def nonsmooth_point(message: str, *, run_id: str | None = None, **details: Any) -> ServiceError:
    return ServiceError(ErrorCategory.NONSMOOTH_POINT, message, details=details, run_id=run_id)
