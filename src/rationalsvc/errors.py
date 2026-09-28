"""Error taxonomy shared by every module.

Categories are deliberately closed-set strings (``ErrorCode``) so that API
clients and tests can branch on an exact failure class rather than parsing
human-readable messages.  Each exception carries a machine-readable
``category`` and, where relevant, structured ``details`` used for replay and
diagnosis.
"""
from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """All failure classes the service can report.

    INPUT_*       malformed payload at the system boundary
    STATE_CONFLICT incompatible options / inconsistent request state
    BUDGET_EXHAUSTED digit/complexity budget consumed before completion
    COMPUTATION_FAILED unexpected internal failure (with diagnostic payload)
    """

    # --- input errors (client fault, 4xx) ---
    INPUT_MALFORMED = "input_malformed"
    INPUT_EMPTY = "input_empty"
    INPUT_SHAPE_MISMATCH = "input_shape_mismatch"
    INPUT_NOT_REPRESENTABLE = "input_not_representable"
    INPUT_PRECISION_UNSUPPORTED = "input_precision_unsupported"

    # --- state conflict (client fault, 409) ---
    STATE_CONFLICT = "state_conflict"

    # --- resource exhaustion (422/507) ---
    BUDGET_EXHAUSTED = "budget_exhausted"

    # --- computation failure (500) ---
    COMPUTATION_FAILED = "computation_failed"


class ServiceError(Exception):
    """Base class for all structured service errors."""

    category: ErrorCode = ErrorCode.COMPUTATION_FAILED
    http_status: int = 500

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {
            "error": self.category.value,
            "message": self.message,
            "details": self.details,
        }


class InputError(ServiceError):
    http_status = 400


class InputMalformed(InputError):
    category = ErrorCode.INPUT_MALFORMED


class InputEmpty(InputError):
    category = ErrorCode.INPUT_EMPTY


class InputShapeMismatch(InputError):
    category = ErrorCode.INPUT_SHAPE_MISMATCH


class InputNotRepresentable(InputError):
    category = ErrorCode.INPUT_NOT_REPRESENTABLE


class InputPrecisionUnsupported(InputError):
    category = ErrorCode.INPUT_PRECISION_UNSUPPORTED


class StateConflict(ServiceError):
    category = ErrorCode.STATE_CONFLICT
    http_status = 409


class BudgetExhausted(ServiceError):
    """Raised when a digit/complexity budget is hit mid-computation.

    ``progress`` is a :class:`rationalsvc.runner.ProgressSnapshot`-like dict
    containing enough intermediate state to diagnose and replay the run.
    """

    category = ErrorCode.BUDGET_EXHAUSTED
    http_status = 422

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message, details)
        self.progress = (details or {}).get("progress")


class ComputationFailed(ServiceError):
    category = ErrorCode.COMPUTATION_FAILED
    http_status = 500
