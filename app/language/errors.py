"""Typed errors shared across language / engine / API layers."""

from __future__ import annotations


class DatalogError(Exception):
    """Base class.  ``category`` is a stable machine-readable code reported
    in API responses so callers can assert on failure *classes*."""

    category = "datalog_error"


class CompilationError(DatalogError):
    category = "compilation_error"


class UnsafeVariableError(CompilationError):
    category = "unsafe_variable"


class NegationCycleError(CompilationError):
    category = "negation_cycle"


class ArityError(CompilationError):
    category = "arity_mismatch"


class ParseFailure(DatalogError):
    category = "parse_error"


class QueryError(DatalogError):
    category = "query_error"


class UnknownPredicateError(QueryError):
    category = "unknown_predicate"
