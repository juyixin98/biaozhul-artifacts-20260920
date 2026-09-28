"""Structured error taxonomy.

Errors are deliberately *not* all reported as success: every failure class
carries a stable ``code`` used by the API layer and by test assertions of
failure categories.
"""

from __future__ import annotations


class WfstError(Exception):
    """Base class for all service errors."""

    code = "wfst_error"


class SpecError(WfstError):
    """Corpus/specification document is malformed."""

    code = "spec_error"

    def __init__(self, message: str, *, path: str = ""):
        super().__init__(message)
        self.path = path


class CycleError(WfstError):
    """A forbidden cycle (epsilon loop or negative-cost cycle) was detected."""

    code = "cycle_error"

    def __init__(self, message: str, *, states: tuple[int, ...] = ()):
        super().__init__(message)
        self.states = tuple(states)


class BudgetExhausted(WfstError):
    """Search terminated early because its expansion budget ran out.

    The result is *incomplete*: callers must treat it as an error, never as a
    successful (possibly truncated) answer.
    """

    code = "budget_exhausted"

    def __init__(self, message: str, *, expansions: int, budget: int):
        super().__init__(message)
        self.expansions = expansions
        self.budget = budget


class NotFoundError(WfstError):
    """Referenced transducer/corpus is not present in the index."""

    code = "not_found"


class QueryError(WfstError):
    """Query request is invalid."""

    code = "query_error"


class IndexError_(WfstError):
    """Persistence layer failure."""

    code = "index_error"
