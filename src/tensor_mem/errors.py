"""Error taxonomy for the planner/executor boundary.

Every failure raised across a module boundary is one of the categories below.
The HTTP layer maps each category to a distinct status code, and tests assert
on :attr:`PlannerError.category` rather than on message text.
"""

from __future__ import annotations


class PlannerError(Exception):
    """Base class for all domain errors.

    Attributes:
        category: Stable machine-readable failure category.
        details: Structured context safe to surface to clients.
    """

    category: str = "internal_error"

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, object] = details

    def to_dict(self) -> dict[str, object]:
        return {"category": self.category, "message": self.message, "details": self.details}


class InputValidationError(PlannerError):
    """Malformed request input (bad shape, dtype, unknown reference)."""

    category = "input_error"


class GraphValidationError(PlannerError):
    """Structurally invalid graph (cycle, missing node, type mismatch)."""

    category = "graph_validation_error"


class StateConflictError(PlannerError):
    """Illegal training-state transition or output handle used twice."""

    category = "state_conflict"


class ResourceExhaustedError(PlannerError):
    """Peak requirement exceeds the configured memory budget."""

    category = "resource_exhausted"


class BindCapacityError(PlannerError):
    """Dynamic shape grew beyond an existing buffer pool capacity.

    This is the signal the executor catches to trigger replanning. It is not
    itself a client-visible failure: reusing an undersized buffer out of range
    is forbidden, so the current schedule is invalid and must be rebuilt.
    """

    category = "bind_capacity"


class ComputationError(PlannerError):
    """An op kernel raised or produced a non-finite result."""

    category = "computation_failure"
