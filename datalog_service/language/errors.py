"""Typed errors for every failure category the service can produce.

Compilation gathers *all* issues it can (unsafe variables in several rules
are reported together); parsing fails fast on the first lexical/syntactic
error because no AST can be recovered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    location: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "location": self.location}


class DatalogError(Exception):
    """Base class. ``code`` is the stable, machine-readable failure category."""

    code = "DATALOG_ERROR"

    def __init__(self, message: str, *, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ParseError(DatalogError):
    code = "PARSE_ERROR"


class CompileError(DatalogError):
    """One or more compile-time issues (safety, arity, stratification)."""

    code = "COMPILE_ERROR"

    def __init__(self, issues: List[Issue]):
        self.issues = issues
        message = "; ".join(f"[{i.code}] {i.message}" for i in issues)
        super().__init__(message, details={"issues": [i.to_dict() for i in issues]})

    @staticmethod
    def single(code: str, message: str, **location: Any) -> "CompileError":
        return CompileError([Issue(code, message, location)])


class QueryError(DatalogError):
    code = "QUERY_ERROR"


class StateError(DatalogError):
    code = "STATE_ERROR"
