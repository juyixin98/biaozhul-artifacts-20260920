"""Error hierarchy for the resflow toolchain."""
from __future__ import annotations

from typing import Optional

from .locations import Span


class ResflowError(Exception):
    """Base class for all resflow errors. Carries an optional source span."""

    code = "E-RESFLOW"

    def __init__(self, message: str, span: Optional[Span] = None):
        super().__init__(message)
        self.message = message
        self.span = span

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "span": self.span.to_dict() if self.span is not None else None,
        }

    def format(self) -> str:
        if self.span is None:
            return f"{self.code}: {self.message}"
        return f"{self.code}: {self.message} ({self.span.short})"


class LexError(ResflowError):
    code = "E-LEX"


class ParseError(ResflowError):
    code = "E-PARSE"


class SemanticError(ResflowError):
    """Resolved after parsing: unknown function, wrong arity, redeclaration, ..."""

    code = "E-SEM"


class AnalysisError(ResflowError):
    """Errors raised while running the analysis itself (never a source finding)."""

    code = "E-ANALYSIS"
