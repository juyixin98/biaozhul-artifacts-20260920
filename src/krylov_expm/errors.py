"""Failure categories shared by validation, kernel, and service layers."""
from __future__ import annotations

from enum import Enum


class FailureCategory(str, Enum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    NOT_CONVERGED = "NOT_CONVERGED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ExpmvFailure(Exception):
    """Domain failure carrying a machine-readable category and details."""

    def __init__(self, category: FailureCategory, message: str, details: dict | None = None):
        super().__init__(message)
        self.category = category
        self.message = message
        self.details = details or {}
