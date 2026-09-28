"""Explicit failure categories.

The service and the verification scripts never collapse an exception or an
unknown state into a success response.  Every error carries a stable
machine-readable ``code`` (the *failure category*) plus a human message.
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    # --- input validation (boundary) ---
    SHAPE_INVALID = "shape_invalid"
    SIZE_LIMIT = "size_limit"
    EMPTY_INPUT = "empty_input"
    COMPLEX_SPEC_INVALID = "complex_spec_invalid"
    NONFINITE_INPUT = "nonfinite_input"
    FIRST_ELEMENT_MISMATCH = "first_element_mismatch"
    BATCH_LENGTH_MISMATCH = "batch_length_mismatch"
    PRECISION_UNSUPPORTED = "precision_unsupported"
    # --- kernel / planning ---
    KERNEL_FAILED = "kernel_failed"
    ALIASING_UNSAFE = "aliasing_unsafe"
    NUMERIC_ACCURACY = "numeric_accuracy"
    # --- service ---
    BAD_REQUEST = "bad_request"
    INTERNAL_ERROR = "internal_error"


class ToeplitzError(Exception):
    """Base class for all categorised failures."""

    def __init__(self, code: ErrorCode, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = ErrorCode(code)
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict:
        return {
            "ok": False,
            "error": {
                "code": self.code.value,
                "message": self.message,
                "details": self.details,
            },
        }


class InputError(ToeplitzError):
    """Validation failure at the system boundary (HTTP 422)."""


class KernelError(ToeplitzError):
    """Compute kernel failure (HTTP 500 / explicit error report)."""


class AccuracyError(ToeplitzError):
    """Computed result failed its numerical evidence check."""
