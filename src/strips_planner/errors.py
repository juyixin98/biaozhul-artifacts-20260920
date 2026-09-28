"""Typed error contract shared by every layer.

Categories are stable strings surfaced through the API envelope and persisted
with each run, so callers can distinguish:

* ``INPUT_INVALID``       - request shape / JSON / value errors
* ``INVALID_PROBLEM``     - syntactically well-formed but semantically invalid
                            STRIPS problem
* ``STATE_CONFLICT``      - an action was executed in a state that does not
                            satisfy its preconditions (independent executor)
* ``RESOURCE_LIMIT``      - search stopped at a declared bound; solvability is
                            unknown, the bound is reported
* ``NOT_FOUND``           - unknown run id
* ``COMPUTATION_FAILED``  - internal invariant violation / storage failure
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ErrorCategory:
    INPUT_INVALID = "INPUT_INVALID"
    INVALID_PROBLEM = "INVALID_PROBLEM"
    STATE_CONFLICT = "STATE_CONFLICT"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    NOT_FOUND = "NOT_FOUND"
    COMPUTATION_FAILED = "COMPUTATION_FAILED"


class StripsError(Exception):
    category: str = ErrorCategory.COMPUTATION_FAILED
    code: str = "UNEXPECTED_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class ProblemParseError(StripsError):
    category = ErrorCategory.INPUT_INVALID
    code = "SYNTAX_ERROR"


class ProblemValidationError(StripsError):
    category = ErrorCategory.INVALID_PROBLEM
    code = "INVALID_PROBLEM"

    def __init__(self, issues: list["ValidationIssue"], message: str | None = None):
        self.issues = issues
        super().__init__(
            message or f"problem validation failed with {len(issues)} issue(s)",
            details={"issues": [i.to_dict() for i in issues]},
        )


class StateConflictError(StripsError):
    category = ErrorCategory.STATE_CONFLICT
    code = "PRECONDITION_NOT_SATISFIED"


class ResourceLimitError(StripsError):
    category = ErrorCategory.RESOURCE_LIMIT
    code = "SEARCH_LIMIT_REACHED"


class RunNotFoundError(StripsError):
    category = ErrorCategory.NOT_FOUND
    code = "RUN_NOT_FOUND"


class StorageError(StripsError):
    category = ErrorCategory.COMPUTATION_FAILED
    code = "STORAGE_FAILURE"


class ComputationError(StripsError):
    category = ErrorCategory.COMPUTATION_FAILED
    code = "COMPUTATION_FAILED"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    location: str
    message: str
    severity: str = "error"  # "error" blocks; "warning" is reported but accepted

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "location": self.location,
            "message": self.message,
            "severity": self.severity,
        }


# Stable issue / failure codes used across parser, validator and executor.
class IssueCode:
    SYNTAX_ERROR = "SYNTAX_ERROR"
    NOT_A_STRING = "NOT_A_STRING"
    NOT_AN_OBJECT = "NOT_AN_OBJECT"
    NOT_A_LIST = "NOT_A_LIST"
    MISSING_FIELD = "MISSING_FIELD"
    UNKNOWN_FIELD = "UNKNOWN_FIELD"
    DUPLICATE_TYPE = "DUPLICATE_TYPE"
    DUPLICATE_OBJECT = "DUPLICATE_OBJECT"
    DUPLICATE_PREDICATE = "DUPLICATE_PREDICATE"
    DUPLICATE_PARAMETER = "DUPLICATE_PARAMETER"
    DUPLICATE_ACTION = "DUPLICATE_ACTION"
    UNKNOWN_TYPE = "UNKNOWN_TYPE"
    UNKNOWN_PREDICATE = "UNKNOWN_PREDICATE"
    UNKNOWN_REFERENCE = "UNKNOWN_REFERENCE"
    ARITY_MISMATCH = "ARITY_MISMATCH"
    TYPE_MISMATCH = "TYPE_MISMATCH"
    UNUSED_PARAMETER = "UNUSED_PARAMETER"
    INVALID_COST = "INVALID_COST"
    ADD_DELETE_CONFLICT = "ADD_DELETE_CONFLICT"
    STATIC_PREDICATE_MODIFIED = "STATIC_PREDICATE_MODIFIED"
    GROUNDING_TOO_LARGE = "GROUNDING_TOO_LARGE"
    NEGATIVE_EFFECT_NOT_ALLOWED = "NEGATIVE_EFFECT_NOT_ALLOWED"
    INVALID_GOAL = "INVALID_GOAL"
    UNKNOWN_ACTION = "UNKNOWN_ACTION"
    MISSING_PRECONDITION = "MISSING_PRECONDITION"
    NEGATIVE_PRECONDITION_VIOLATED = "NEGATIVE_PRECONDITION_VIOLATED"
    GOAL_NOT_REACHED = "GOAL_NOT_REACHED"
    PLAN_NOT_VALIDATED = "PLAN_NOT_VALIDATED"
