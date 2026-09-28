"""Error taxonomy.

Every failure raised by the service carries one of four categories so that
callers (and tests) can distinguish *why* a request failed instead of only
seeing "the endpoint was called":

- INPUT_INVALID       : the numeric input itself is rejected (zero polynomial,
                        non-finite coefficient, degree 0, malformed shape)
- STATE_CONFLICT      : the request conflicts with recorded state (run_id
                        replayed with a different payload)
- RESOURCE_EXHAUSTED  : the request exceeds configured resource limits
                        (degree cap, iteration cap)
- COMPUTATION_FAILED  : the numeric kernel failed on otherwise valid input
                        (eigendecomposition failure, NaN/Inf in iterates)
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Any, Optional


class ErrorCategory(str, Enum):
    INPUT_INVALID = "input_invalid"
    STATE_CONFLICT = "state_conflict"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    COMPUTATION_FAILED = "computation_failed"


# HTTP status mapping is defined here, next to the taxonomy, so the API layer
# cannot drift away from the kernel/service layers.
HTTP_STATUS = {
    ErrorCategory.INPUT_INVALID: 422,
    ErrorCategory.STATE_CONFLICT: 409,
    ErrorCategory.RESOURCE_EXHAUSTED: 413,
    ErrorCategory.COMPUTATION_FAILED: 500,
}


class PolyRootsError(Exception):
    """Base exception for all expected failures of the service."""

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        detail: Optional[Any] = None,
        run_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.detail = detail
        self.run_id = run_id

    def to_dict(self) -> dict:
        return {
            "category": self.category.value,
            "message": self.message,
            "detail": _json_safe(self.detail),
            "run_id": self.run_id,
        }


def _json_safe(value: Any) -> Any:
    """Make error details strict-JSON-safe: non-finite floats (which are
    often the very reason for the error) become their repr strings."""
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


class InputInvalidError(PolyRootsError):
    def __init__(self, message: str, *, detail: Any = None, run_id: str = None):
        super().__init__(ErrorCategory.INPUT_INVALID, message, detail=detail, run_id=run_id)


class StateConflictError(PolyRootsError):
    def __init__(self, message: str, *, detail: Any = None, run_id: str = None):
        super().__init__(ErrorCategory.STATE_CONFLICT, message, detail=detail, run_id=run_id)


class ResourceExhaustedError(PolyRootsError):
    def __init__(self, message: str, *, detail: Any = None, run_id: str = None):
        super().__init__(ErrorCategory.RESOURCE_EXHAUSTED, message, detail=detail, run_id=run_id)


class ComputationFailedError(PolyRootsError):
    def __init__(self, message: str, *, detail: Any = None, run_id: str = None):
        super().__init__(ErrorCategory.COMPUTATION_FAILED, message, detail=detail, run_id=run_id)
