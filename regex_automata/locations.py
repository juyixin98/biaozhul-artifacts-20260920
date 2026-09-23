"""Source-code position tracking.

The entire tool-chain (lexer -> parser -> compiler) carries offsets into
the *pattern source* on every token and AST node.  Positions are
**Unicode code-point offsets** (Python string indices), half-open
``[start, end)``.  Matching itself also operates on code points; see
``regex_automata.matcher``.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    """Half-open code-point range ``[start, end)`` in pattern source."""

    start: int
    end: int

    def merge(self, other: "Span") -> "Span":
        return Span(min(self.start, other.start), max(self.end, other.end))


def line_col(source: str, offset: int) -> tuple[int, int]:
    """Map a code-point offset to a 1-based ``(line, column)``."""
    offset = max(0, min(offset, len(source)))
    prefix = source[:offset]
    line = prefix.count("\n") + 1
    column = len(prefix) - prefix.rfind("\n")
    return line, column


def render_snippet(source: str, span: Span, width: int = 72) -> str:
    """Return a two-line ``source`` / ``^^^^`` indicator for ``span``."""
    if not source:
        return ""
    lines = source.splitlines() or [""]
    s = max(0, min(span.start, len(source)))
    e = max(s, min(span.end, len(source)))
    line_no = source[:s].count("\n")
    line_text = lines[line_no]
    line_start = source.rfind("\n", 0, s) + 1
    column = s - line_start
    caret_len = max(1, e - s - source[s:e].count("\n"))
    if len(line_text) > width:
        cut = max(0, min(column, len(line_text) - width))
        shown = line_text[cut : cut + width]
        column -= cut
        prefix = "…"
    else:
        shown = line_text
        prefix = ""
    return f"{prefix}{shown}\n{' ' * (len(prefix) + column)}{'^' * caret_len}"
