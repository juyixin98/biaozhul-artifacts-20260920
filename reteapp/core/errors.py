"""Core error taxonomy.

Distinct failure categories let the API and tests assert *what* went wrong
instead of collapsing every problem into a generic success/error flag.
"""

from __future__ import annotations


class ReteError(Exception):
    """Base class for all engine errors."""

    category = "rete_error"


class FactValidationError(ReteError):
    """Inserted payload is not a well-formed fact."""

    category = "fact_validation_error"


class UnknownFactError(ReteError):
    """Retract/lookup references a working-memory id that does not exist."""

    category = "unknown_fact_error"


class RuleEvaluationError(ReteError):
    """A constraint could not be evaluated (undefined comparison etc.).

    The insert is rejected transactionally - never silently treated as a
    non-match.
    """

    category = "rule_evaluation_error"
