"""Source positions and error types.

Every token and AST node carries a :class:`Span` (byte offsets + 1-based
line/column) so that resolver/parser/runtime errors can point back at the
exact source text.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    """A half-open source range ``[start, end)`` with 1-based line/column."""

    start: int
    end: int
    line: int
    col: int
    end_line: int
    end_col: int
    source: str = ""

    def __str__(self) -> str:
        if self.end_line == self.line:
            return f"{self.line}:{self.col}-{self.end_col}"
        return f"{self.line}:{self.col}-{self.end_line}:{self.end_col}"

    def snippet(self) -> str:
        """Return the covered source line(s) with a caret underline."""
        if not self.source:
            return ""
        lines = self.source.splitlines()
        if not lines or self.line - 1 >= len(lines):
            return ""
        if self.end_line == self.line:
            text = lines[self.line - 1]
            width = max(1, self.end_col - self.col)
            return f"{text}\n{' ' * (self.col - 1)}{'^' * width}"
        return lines[self.line - 1] + "\n" + " " * (self.col - 1) + "^"


class SclError(Exception):
    """Base class for all toolchain errors."""

    def __init__(self, message: str, span: "Span | None" = None):
        self.message = message
        self.span = span
        if span is not None and span.source:
            loc = f" at {span}"
            snip = "\n" + span.snippet()
        elif span is not None:
            loc = f" at {span}"
            snip = ""
        else:
            loc = ""
            snip = ""
        super().__init__(f"{message}{loc}{snip}")


class CompileError(SclError):
    """Lexing, parsing, or lexical-analysis error (program never runs)."""


class RuntimeError_(SclError):
    """An error raised while executing a program."""
