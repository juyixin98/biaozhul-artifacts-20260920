"""Typed failure categories.

Failures are never flattened into a generic success.  Every kernel/search
failure maps to an explicit :class:`PlannerError` category that survives
serialization to the API and the run log.
"""
from __future__ import annotations

from enum import Enum


class ErrorCategory(str, Enum):
    """Exhaustive failure categories for the planning pipeline."""

    VALIDATION_ERROR = "validation_error"
    """Input failed contract validation (range, consistency)."""

    EFFECT_TOO_SMALL = "effect_too_small"
    """Effect is indistinguishable from zero at the requested precision/cap."""

    APPROXIMATION_INVALID = "approximation_invalid"
    """Normal approximation is not trustworthy (e.g. low expected counts)."""

    EXACT_CAP_EXCEEDED = "exact_cap_exceeded"
    """Exact integer search exceeded its configured sample-size cap."""

    NONCENTRALITY_ERROR = "noncentrality_error"
    """Non-central distribution evaluation failed."""

    NO_FEASIBLE_SAMPLE = "no_feasible_sample"
    """Integer search could not find an n achieving target power."""

    NUMERIC_FAILURE = "numeric_failure"
    """Underlying numeric routine failed (SciPy / search)."""

    SIMULATION_ERROR = "simulation_error"
    """Monte-Carlo evidence generation failed."""

    PERSISTENCE_ERROR = "persistence_error"
    """SQLite read/write failed."""


class PlannerError(Exception):
    """Base error carrying a machine-readable category and contextual detail."""

    category: ErrorCategory = ErrorCategory.NUMERIC_FAILURE

    def __init__(self, message: str, *, category: ErrorCategory | None = None, details: dict | None = None):
        super().__init__(message)
        if category is not None:
            self.category = category
        self.details = details or {}

    def to_dict(self) -> dict:
        return {
            "category": self.category.value,
            "message": str(self),
            "details": self.details,
        }


class ValidationError(PlannerError):
    category = ErrorCategory.VALIDATION_ERROR


class EffectTooSmallError(PlannerError):
    category = ErrorCategory.EFFECT_TOO_SMALL


class ApproximationInvalidError(PlannerError):
    category = ErrorCategory.APPROXIMATION_INVALID


class ExactCapExceededError(PlannerError):
    category = ErrorCategory.EXACT_CAP_EXCEEDED


class NoncentralityError(PlannerError):
    category = ErrorCategory.NONCENTRALITY_ERROR


class NoFeasibleSampleError(PlannerError):
    category = ErrorCategory.NO_FEASIBLE_SAMPLE


class NumericFailureError(PlannerError):
    category = ErrorCategory.NUMERIC_FAILURE


class SimulationError(PlannerError):
    category = ErrorCategory.SIMULATION_ERROR


class PersistenceError(PlannerError):
    category = ErrorCategory.PERSISTENCE_ERROR
