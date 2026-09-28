"""Domain error hierarchy.

Every error raised across module boundaries carries:
* ``code``     -- stable machine readable code (E_<CATEGORY>_<REASON>)
* ``category`` -- one of :data:`ErrorCategory`, used for logs and HTTP mapping
* ``context``  -- structured details useful for replaying the failure
* ``run_id``   -- attached by the service layer once a run is known
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class ErrorCategory:
    INPUT_ERROR = "input_error"
    STATE_CONFLICT = "state_conflict"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    COMPUTATION_FAILURE = "computation_failure"


class ActivationRecomputeError(Exception):
    """Base class for all expected, classified failures."""

    category: str = ErrorCategory.INPUT_ERROR
    default_code: str = "E_UNKNOWN"

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        run_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self.context: Dict[str, Any] = dict(context or {})
        self.run_id = run_id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "category": self.category,
            "message": self.message,
            "context": self.context,
            "run_id": self.run_id,
        }


class InvalidInputError(ActivationRecomputeError):
    """Malformed request/graph/value. Caller (client) may fix and retry."""

    category = ErrorCategory.INPUT_ERROR
    default_code = "E_INPUT_INVALID"


class StateConflictError(ActivationRecomputeError):
    """Valid input but the training/execution lifecycle was violated."""

    category = ErrorCategory.STATE_CONFLICT
    default_code = "E_STATE_CONFLICT"


class ResourceExhaustedError(ActivationRecomputeError):
    """The memory budget (or another hard resource limit) cannot be met."""

    category = ErrorCategory.RESOURCE_EXHAUSTED
    default_code = "E_RESOURCE_EXHAUSTED"


class ComputationError(ActivationRecomputeError):
    """Execution started but numerical evaluation failed or diverged."""

    category = ErrorCategory.COMPUTATION_FAILURE
    default_code = "E_COMP_FAILURE"
