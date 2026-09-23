"""Source locations and compiler diagnostics.

Every token, AST node and IR instruction produced from source keeps a
``loc`` pointing back at the original file (byte offset + 1-based line and
column) so downstream phases can report where things came from.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Loc:
    """A half-open source span [offset, end_offset)."""

    file: str
    offset: int
    end_offset: int
    line: int
    col: int
    end_line: int = 0
    end_col: int = 0

    def short(self) -> str:
        return f"{self.file}:{self.line}:{self.col}"

    def merge(self, other: "Loc") -> "Loc":
        if self.offset <= other.offset:
            lo, hi = self, other
        else:
            lo, hi = other, self
        return Loc(
            file=lo.file,
            offset=lo.offset,
            end_offset=hi.end_offset,
            line=lo.line,
            col=lo.col,
            end_line=hi.end_line,
            end_col=hi.end_col,
        )

    def to_json(self) -> dict:
        return {
            "file": self.file,
            "offset": self.offset,
            "end_offset": self.end_offset,
            "line": self.line,
            "col": self.col,
            "end_line": self.end_line or self.line,
            "end_col": self.end_col or self.col,
        }


class MiniError(Exception):
    """Base class for all user-facing compiler errors."""

    def __init__(self, message: str, loc: Loc | None = None):
        super().__init__(message)
        self.message = message
        self.loc = loc

    def render(self, source: str | None = None) -> str:
        if self.loc is None:
            return f"error: {self.message}"
        head = f"{self.loc.short()}: error: {self.message}"
        if source is None:
            return head
        lines = source.splitlines()
        if not (1 <= self.loc.line <= len(lines)):
            return head
        line_text = lines[self.loc.line - 1]
        caret = " " * (self.loc.col - 1) + "^"
        return f"{head}\n  {line_text}\n  {caret}"


class LexError(MiniError):
    pass


class ParseError(MiniError):
    pass


class LoweringError(MiniError):
    pass


class IRError(MiniError):
    pass


class VerifyError(MiniError):
    pass
