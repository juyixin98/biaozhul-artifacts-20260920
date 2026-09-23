"""Error types carrying source locations (1-based line/column, 0-based offset)."""

from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    """A source span.

    offsets are 0-based byte/character indices into the source string;
    line/col are 1-based, with col counting characters from the line start.
    """

    start_offset: int
    end_offset: int
    line: int
    col: int

    def to_dict(self) -> dict:
        return {
            "start": self.start_offset,
            "end": self.end_offset,
            "line": self.line,
            "col": self.col,
        }

    def __str__(self) -> str:
        return f"{self.line}:{self.col}"


class IvlError(Exception):
    """Base class for all toolchain errors."""

    def __init__(self, message: str, span: Span | None = None):
        self.message = message
        self.span = span
        loc = f" ({span})" if span is not None else ""
        super().__init__(message + loc)


class LexError(IvlError):
    pass


class ParseError(IvlError):
    pass


class AnalysisError(IvlError):
    pass


class RuntimeErr(Exception):
    """Concrete-execution runtime error (division by zero / index out of range)."""

    def __init__(self, kind: str, message: str, op: str | None = None,
                 span: Span | None = None):
        self.kind = kind          # "div_by_zero" | "index_out_of_bounds"
        self.op = op              # "/" , "%", "[]"
        self.span = span
        super().__init__(message)
