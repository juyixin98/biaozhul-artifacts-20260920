"""Source positions and structured compiler errors.

Every AST node and IR instruction carries a :class:`Span` so that runtime
errors and service responses can point back at the user's program.
Positions use 1-based lines/columns; columns count Unicode code points.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    """A half-open-ish source range recorded as (start, end) pairs.

    Attributes mirror the JSON wire format: (line, col) for both endpoints,
    plus the absolute character ``offset`` of the start.
    """

    start_line: int
    start_col: int
    end_line: int
    end_col: int
    offset: int = 0
    length: int = 0
    synthetic: bool = False

    def to_json(self) -> dict:
        return {
            "start": {"line": self.start_line, "col": self.start_col},
            "end": {"line": self.end_line, "col": self.end_col},
            "offset": self.offset,
            "length": self.length,
            "synthetic": self.synthetic,
        }

    def merge(self, other: "Span") -> "Span":
        """Smallest span covering both ranges (lexicographic ordering)."""
        a, b = (self, other) if _key(self) <= _key(other) else (other, self)
        return Span(
            a.start_line,
            a.start_col,
            b.end_line,
            b.end_col,
            a.offset,
            max(0, b.offset + b.length - a.offset),
            synthetic=self.synthetic and other.synthetic,
        )


def _key(s: Span) -> tuple[int, int]:
    return (s.start_line, s.start_col)


def synthetic_span() -> Span:
    """Span for compiler-generated entities with no source location."""
    return Span(0, 0, 0, 0, synthetic=True)


class LangError(Exception):
    """Structured error produced by any pipeline stage.

    ``stage`` is one of ``lex`` / ``parse`` / ``ir`` / ``ssa`` / ``runtime`` /
    ``service``.  ``span`` may be ``None`` when no location applies.
    """

    def __init__(self, message: str, stage: str = "ir", span: Span | None = None):
        super().__init__(message)
        self.message = message
        self.stage = stage
        self.span = span

    def to_json(self) -> dict:
        out: dict = {"stage": self.stage, "message": self.message}
        if self.span is not None:
            out["span"] = self.span.to_json()
        return out


def span_between(left: Span | None, right: Span | None) -> Span | None:
    if left is not None and right is not None:
        return left.merge(right)
    return left or right
