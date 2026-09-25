"""Source locations.

Every token, AST node and CFG node carries a :class:`Span` so that diagnostics
and findings can point back at the exact characters in the analyzed source.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import List


@dataclass(frozen=True)
class Position:
    """Zero-based byte-independent character offset, converted to 1-based lines."""

    offset: int
    line: int
    column: int  # 1-based column in characters

    def human(self) -> str:
        return f"{self.line}:{self.column}"


@dataclass(frozen=True)
class Span:
    """A half-open ``[start, end)`` character interval inside one file."""

    filename: str
    start: Position
    end: Position

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "start": {"offset": self.start.offset, "line": self.start.line, "column": self.start.column},
            "end": {"offset": self.end.offset, "line": self.end.line, "column": self.end.column},
        }

    @property
    def short(self) -> str:
        return f"{self.filename}:{self.start.human()}"


class SourceFile:
    """Holds source text and translates character offsets to line/column."""

    def __init__(self, text: str, filename: str):
        self.text = text
        self.filename = filename
        self.line_starts: List[int] = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                self.line_starts.append(i + 1)

    def pos(self, offset: int) -> Position:
        # Binary search for the largest line start <= offset.
        lo, hi = 0, len(self.line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.line_starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        line = lo  # 0-based line index
        return Position(offset=offset, line=line + 1, column=offset - self.line_starts[line] + 1)

    def span(self, start: int, end: int) -> Span:
        return Span(self.filename, self.pos(start), self.pos(end))

    def line_text(self, line: int) -> str:
        """Return the text of the given 1-based line (without newline)."""
        idx = line - 1
        if idx < 0 or idx >= len(self.line_starts):
            return ""
        start = self.line_starts[idx]
        if idx + 1 < len(self.line_starts):
            end = self.line_starts[idx + 1] - 1
        else:
            end = len(self.text)
        return self.text[start:end]


def merge_spans(a: Span, b: Span) -> Span:
    """Smallest span covering both spans (assumed to be in the same file)."""
    start = a.start if a.start.offset <= b.start.offset else b.start
    end = a.end if a.end.offset >= b.end.offset else b.end
    return replace(a, start=start, end=end)
