"""Error taxonomy shared across all module boundaries.

Every failure surfaced by the system is classified into exactly one of four
categories so that callers, logs and tests can distinguish them:

- ``INPUT_ERROR``         — the caller supplied invalid data (bad regex syntax,
  a rule that can match the empty string, unknown ruleset, malformed body).
- ``STATE_CONFLICT``    — the request conflicts with persisted state (e.g. a
  ruleset name that already exists).
- ``RESOURCE_EXHAUSTED``— a configured limit was hit (too many rules, automaton
  state explosion, oversized input text).
- ``COMPUTATION_FAILURE``— an unexpected internal failure.
"""

from __future__ import annotations

import enum
from typing import Any, Optional


class ErrorCategory(str, enum.Enum):
    INPUT_ERROR = "INPUT_ERROR"
    STATE_CONFLICT = "STATE_CONFLICT"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    COMPUTATION_FAILURE = "COMPUTATION_FAILURE"


_DEFAULT_HTTP_STATUS = {
    ErrorCategory.INPUT_ERROR: 400,
    ErrorCategory.STATE_CONFLICT: 409,
    ErrorCategory.RESOURCE_EXHAUSTED: 413,
    ErrorCategory.COMPUTATION_FAILURE: 500,
}


class AppError(Exception):
    """An error carrying a category, a human message and an HTTP status."""

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        detail: Optional[dict[str, Any]] = None,
        http_status: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.detail = detail or {}
        self._http_status = http_status

    @property
    def http_status(self) -> int:
        if self._http_status is not None:
            return self._http_status
        return _DEFAULT_HTTP_STATUS[self.category]

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "message": self.message,
            "detail": self.detail,
        }


def input_error(message: str, **kwargs: Any) -> AppError:
    return AppError(ErrorCategory.INPUT_ERROR, message, **kwargs)


def state_conflict(message: str, **kwargs: Any) -> AppError:
    return AppError(ErrorCategory.STATE_CONFLICT, message, **kwargs)


def resource_exhausted(message: str, **kwargs: Any) -> AppError:
    return AppError(ErrorCategory.RESOURCE_EXHAUSTED, message, **kwargs)


def computation_failure(message: str, **kwargs: Any) -> AppError:
    return AppError(ErrorCategory.COMPUTATION_FAILURE, message, **kwargs)
