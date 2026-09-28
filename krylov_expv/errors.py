"""Typed failures with stable categories.

Every abnormal exit of the pipeline carries a machine-readable ``category``
so that API responses, logs, and tests can assert on the failure class
instead of matching message text.
"""

from __future__ import annotations


class ExpvError(Exception):
    """Base class for all expected, reportable failures."""

    category = "internal_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class InputValidationError(ExpvError):
    """Malformed numerical input (shape, bounds, finiteness, ...)."""

    category = "invalid_input"


class MemoryBudgetExceeded(ExpvError):
    """The request would exceed the configured basis-storage budget."""

    category = "memory_budget_exceeded"


class NotConverged(ExpvError):
    """The step/restart budget was exhausted before reaching tolerance."""

    category = "max_steps_exceeded"


class StepSizeUnderflow(ExpvError):
    """Step halving exceeded its cap without satisfying the tolerance."""

    category = "step_size_underflow"
