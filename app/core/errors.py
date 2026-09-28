"""Typed estimation errors. Every failure the core raises carries one of the
contract :class:`FailureCategory` values so the API can report a precise,
machine-readable reason rather than a generic 500.
"""
from __future__ import annotations

from app.contracts.models import FailureCategory


class EstimationError(Exception):
    def __init__(
        self,
        category: FailureCategory,
        message: str,
        *,
        excluded: list | None = None,
        diagnostics: list | None = None,
        steps: list | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.excluded = excluded or []
        self.diagnostics = diagnostics or []
        self.steps = steps or []
