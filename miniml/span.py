"""Source positions and spans.

Positions are 1-based line/column pairs (column counts Unicode code points).
A span is a half-open-ish range ``[start, end)`` where ``end`` is the position
*just after* the last character of the token/expression.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Pos:
    offset: int  # 0-based byte-ish index into the source string (code points)
    line: int  # 1-based
    col: int  # 1-based

    def __str__(self) -> str:
        return f"{self.line}:{self.col}"


@dataclass(frozen=True)
class Span:
    start: Pos
    end: Pos

    @property
    def lo(self) -> int:
        return self.start.offset

    @property
    def hi(self) -> int:
        return self.end.offset

    @property
    def start_line_col(self) -> tuple[int, int]:
        return self.start.line, self.start.col

    def __str__(self) -> str:
        return f"{self.start}..{self.end}"


def dummy_span() -> Span:
    p = Pos(0, 1, 1)
    return Span(p, p)
