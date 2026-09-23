"""Errors raised by the regex tool-chain, carrying source positions."""
from __future__ import annotations

from dataclasses import dataclass

from .locations import Span, line_col, render_snippet


@dataclass
class RegexError(Exception):
    """Base error for pattern compilation failures."""

    message: str
    span: Span | None = None
    source: str | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.render()

    def render(self) -> str:
        if self.span is None or self.source is None:
            return f"regex error: {self.message}"
        line, col = line_col(self.source, self.span.start)
        header = f"regex error at line {line}, column {col}: {self.message}"
        snippet = render_snippet(self.source, self.span)
        return f"{header}\n{snippet}" if snippet else header


class LexError(RegexError):
    """Invalid token / escape / quantifier syntax in the pattern."""


class ParseError(RegexError):
    """Well-formed tokens but invalid grammar (unbalanced parens, etc.)."""
