"""Error taxonomy for the HVP service.

Every failure raised by the core carries a stable ``category`` so that API
clients, tests and logs can distinguish *why* a request failed:

- ``input_error``        malformed spec, shape/layout mismatch, unknown op
- ``state_conflict``     missing evaluation point, stale state version
- ``resource_exhausted`` graph-size / evaluation / wall-clock budget exceeded
- ``compute_failure``    non-finite intermediate, domain error (e.g. log(x<0))
- ``nonsmooth_point``    kink of abs/relu hit while policy is ``reject``
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCategory(str, Enum):
    INPUT_ERROR = "input_error"
    STATE_CONFLICT = "state_conflict"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    COMPUTE_FAILURE = "compute_failure"
    NONSMOOTH_POINT = "nonsmooth_point"


class HVPError(Exception):
    """Base class for all service errors."""

    category: ErrorCategory = ErrorCategory.INPUT_ERROR

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail: dict[str, Any] = detail or {}
        self.run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "message": self.message,
            "detail": self.detail,
            "run_id": self.run_id,
        }


class InputError(HVPError):
    category = ErrorCategory.INPUT_ERROR


class StateConflictError(HVPError):
    category = ErrorCategory.STATE_CONFLICT


class ResourceExhaustedError(HVPError):
    category = ErrorCategory.RESOURCE_EXHAUSTED


class ComputeFailureError(HVPError):
    category = ErrorCategory.COMPUTE_FAILURE


class NonSmoothError(HVPError):
    category = ErrorCategory.NONSMOOTH_POINT
