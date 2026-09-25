"""Toolchain errors.  Every error carries a source span when one is known."""

from __future__ import annotations

from typing import Optional

from .location import Span


class TaintLangError(Exception):
    """Base class for all TaintLang diagnostics."""

    def __init__(self, message: str, span: Optional[Span] = None):
        self.message = message
        self.span = span
        if span is not None:
            super().__init__(f"{span.start.line}:{span.start.column}: {message}")
        else:
            super().__init__(message)

    def to_dict(self) -> dict:
        d = {"message": self.message, "type": type(self).__name__}
        if self.span is not None:
            d["span"] = self.span.to_dict()
        return d


class LexError(TaintLangError):
    """Illegal character or malformed literal."""


class ParseError(TaintLangError):
    """Syntax error."""


class BuildError(TaintLangError):
    """Semantic error found while lowering AST -> IR.

    Examples: duplicate function/parameter names, call to an unknown
    function with the wrong arity, redeclaring a built-in marker.
    """


class ConfigError(TaintLangError):
    """Invalid analysis configuration."""
