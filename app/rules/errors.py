"""Domain error types with explicit failure categories.

Categories are part of the public evidence contract: callers (tests, API
clients, log analysis) branch on the stable string codes rather than on
exception messages.
"""
from __future__ import annotations


class FailureCategory:
    """Stable machine-readable failure categories."""

    INVALID_INPUT = "INVALID_INPUT"
    SCHEDULE_INVALID = "SCHEDULE_INVALID"
    PRECONDITION_VIOLATION = "PRECONDITION_VIOLATION"
    INVARIANT_VIOLATION = "INVARIANT_VIOLATION"
    GOAL_NOT_REACHED = "GOAL_NOT_REACHED"
    RESOURCE_CONFLICT = "RESOURCE_CONFLICT"
    SIMULTANEOUS_CONFLICT = "SIMULTANEOUS_CONFLICT"
    INFEASIBLE = "INFEASIBLE"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    VERIFICATION_MISMATCH = "VERIFICATION_MISMATCH"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class PlanningError(Exception):
    """Base error carrying a stable ``category`` code and context."""

    category = FailureCategory.INTERNAL_ERROR

    def __init__(self, message: str, *, category: str | None = None, **context: object) -> None:
        super().__init__(message)
        if category is not None:
            self.category = category
        self.context = dict(context)

    def to_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "message": str(self.args[0]) if self.args else "",
            "context": self.context,
        }


class ValidationFailure(PlanningError):
    category = FailureCategory.INVALID_INPUT
