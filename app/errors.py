"""Domain errors for trajectory evaluation.

Every predictable failure (bad input, duplicate timestamps, zero matches,
degenerate alignment, ...) is reported explicitly as a structured 422 error.
The pipeline never pads data with zeros to make an error disappear.
"""

from __future__ import annotations

from typing import Any


class EvaluationError(Exception):
    """A business-logic error mapped to HTTP 422 with a stable error code."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_body(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}
