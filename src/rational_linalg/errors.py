"""Distinguishable error hierarchy for the rational linear algebra service.

The four top-level categories required by the engineering contract are:

* ``INPUT_ERROR``        -- malformed or invalid request data (4xx, caller's fault)
* ``STATE_CONFLICT``     -- request references unknown/superseded run state (409)
* ``RESOURCE_EXHAUSTED`` -- digit/step/operand budget blown mid-computation (507)
* ``COMPUTATION_FAILED`` -- an internal invariant broke (500)

Every error carries a stable machine-readable ``code`` plus enough structured
detail to replay the failure (run_id, stage, budget snapshot).
"""

from __future__ import annotations

from typing import Any


class RationalLinAlgError(Exception):
    """Base class for all service errors."""

    category: str = "COMPUTATION_FAILED"
    code: str = "COMPUTATION_FAILED"
    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "category": self.category,
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


class InputError(RationalLinAlgError):
    category = "INPUT_ERROR"
    http_status = 400

    def __init__(self, message: str, *, code: str = "INPUT_ERROR", details=None):
        super().__init__(message, code=code, details=details)


class StateConflictError(RationalLinAlgError):
    category = "STATE_CONFLICT"
    http_status = 409

    def __init__(self, message: str, *, code: str = "STATE_CONFLICT", details=None):
        super().__init__(message, code=code, details=details)


class ResourceExhaustedError(RationalLinAlgError):
    category = "RESOURCE_EXHAUSTED"
    http_status = 507

    def __init__(
        self,
        message: str,
        *,
        code: str = "DIGIT_BUDGET_EXCEEDED",
        details: dict[str, Any] | None = None,
        progress: "dict[str, Any] | None" = None,
    ) -> None:
        super().__init__(message, code=code, details=details)
        # Partial, still-diagnosable computation state (pivot index, row count,
        # current matrix snapshot, per-stage digit maxima).
        self.progress: dict[str, Any] = progress or {}

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["error"]["progress"] = self.progress
        return payload


class ComputationFailedError(RationalLinAlgError):
    category = "COMPUTATION_FAILED"
    http_status = 500

    def __init__(self, message: str, *, code: str = "COMPUTATION_FAILED", details=None):
        super().__init__(message, code=code, details=details)
