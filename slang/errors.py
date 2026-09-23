"""Error types carrying source locations.

Every phase of the toolchain (lex, parse, analysis/closure conversion, and
runtime) reports diagnostics with the original source position whenever it is
known, so errors can be mapped back to the user's program.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Loc:
    """A half-open source span [offset, end_offset) on a 1-based line/column."""

    line: int
    col: int
    end_line: int
    end_col: int
    offset: int
    end_offset: int

    def short(self) -> str:
        return f"{self.line}:{self.col}"

    def to_dict(self) -> dict:
        return {
            "line": self.line,
            "col": self.col,
            "end_line": self.end_line,
            "end_col": self.end_col,
            "offset": self.offset,
            "end_offset": self.end_offset,
        }


class LangError(Exception):
    """Base class for all Slang diagnostics."""

    phase = "error"

    def __init__(self, message: str, loc: Loc | None = None):
        self.message = message
        self.loc = loc
        super().__init__(message)

    def render(self) -> str:
        where = f" at {self.loc.short()}" if self.loc is not None else ""
        return f"{self.phase} error{where}: {self.message}"

    def to_dict(self) -> dict:
        d = {"phase": self.phase, "message": self.message}
        if self.loc is not None:
            d["loc"] = self.loc.to_dict()
        return d


class LexError(LangError):
    phase = "lex"


class ParseError(LangError):
    phase = "parse"


class CompileError(LangError):
    """Errors from scope analysis / closure conversion (e.g. unbound names)."""

    phase = "compile"


class RuntimeErr(LangError):
    phase = "runtime"
