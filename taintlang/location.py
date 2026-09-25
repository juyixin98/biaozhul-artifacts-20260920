"""Source locations.

All offsets are 0-based byte/character offsets into the source string;
lines and columns are 1-based.  A :class:`Span` covers the half-open range
``[start, end)``.  Every AST and IR node produced by the toolchain carries
one, so findings can point back at the exact source text.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Position:
    offset: int
    line: int
    column: int

    def to_dict(self) -> dict:
        return {"offset": self.offset, "line": self.line, "column": self.column}


@dataclass(frozen=True)
class Span:
    start: Position
    end: Position
    text: str = ""

    def to_dict(self) -> dict:
        return {
            "start": self.start.to_dict(),
            "end": self.end.to_dict(),
            "text": self.text,
        }

    def __str__(self) -> str:
        return f"{self.start.line}:{self.start.column}-{self.end.line}:{self.end.column}"


def _position_at(source: str, offset: int) -> Position:
    """Clamp offset to [0, len(source)] and derive line/column (1-based)."""
    if offset < 0:
        offset = 0
    if offset > len(source):
        offset = len(source)
    line = 1
    col = 1
    for i, ch in enumerate(source):
        if i == offset:
            break
        if ch == "\n":
            line += 1
            col = 1
        else:
            col += 1
    return Position(offset, line, col)


def make_span(source: str, start: int, end: int) -> Span:
    """Build a Span from raw offsets, slicing the covered source text."""
    start = max(0, start)
    end = max(start, end)
    text = source[start:end]
    return Span(_position_at(source, start), _position_at(source, end), text)
