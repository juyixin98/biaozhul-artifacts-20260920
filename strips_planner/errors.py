"""Error taxonomy shared by every module.

Four mutually exclusive failure categories are part of the service contract:

    input_error         - the request/domain/problem is malformed or violates
                          a static rule; the caller must change the input.
    state_conflict      - a dynamic rule is violated against a concrete state
                          (precondition failure, contradictory state).
    resource_exhausted  - a hard resource cap of the *service* was hit while
                          preparing the search (e.g. grounding explosion).
    computation_failed  - the service itself is wrong (e.g. a plan it produced
                          failed independent re-execution).

Exhausting a *search* bound (node/frontier/depth/time) is NOT an error:
search returns status ``unknown`` with a machine-readable reason instead
(see search.SearchResult), honouring the "unknown or bounded" contract.
"""

from __future__ import annotations

from typing import Any

# Categories (also used in the JSON error envelope).
INPUT_ERROR = "input_error"
STATE_CONFLICT = "state_conflict"
RESOURCE_EXHAUSTED = "resource_exhausted"
COMPUTATION_FAILED = "computation_failed"

# Stable error codes.
DOMAIN_NAME_MISSING = "DOMAIN_NAME_MISSING"
ACTION_NAME_MISSING = "ACTION_NAME_MISSING"
ACTION_NAME_DUPLICATE = "ACTION_NAME_DUPLICATE"
ACTION_NAME_INVALID = "ACTION_NAME_INVALID"
PARAM_MISSING = "PARAM_MISSING"
PARAM_DUPLICATE = "PARAM_DUPLICATE"
PARAM_INVALID = "PARAM_INVALID"
LITERAL_MALFORMED = "LITERAL_MALFORMED"
LITERAL_WRONG_TYPE = "LITERAL_WRONG_TYPE"
UNBOUND_VARIABLE = "UNBOUND_VARIABLE"
PREDICATE_ARITY_CONFLICT = "PREDICATE_ARITY_CONFLICT"
PRECONDITION_CONTRADICTION = "PRECONDITION_CONTRADICTION"
EFFECT_ADD_DELETE_CONFLICT = "EFFECT_ADD_DELETE_CONFLICT"
INVALID_COST = "INVALID_COST"
PROBLEM_NAME_MISSING = "PROBLEM_NAME_MISSING"
OBJECT_REQUIRED = "OBJECT_REQUIRED"
OBJECT_DUPLICATE = "OBJECT_DUPLICATE"
OBJECT_INVALID = "OBJECT_INVALID"
OBJECT_UNDECLARED = "OBJECT_UNDECLARED"
INIT_NOT_POSITIVE = "INIT_NOT_POSITIVE"
GOAL_REQUIRED = "GOAL_REQUIRED"
GOAL_CONTRADICTION = "GOAL_CONTRADICTION"
NO_ACTIONS = "NO_ACTIONS"
UNKNOWN_ACTION = "UNKNOWN_ACTION"
ACTION_ARITY_MISMATCH = "ACTION_ARITY_MISMATCH"
GROUNDING_LIMIT = "GROUNDING_LIMIT"
PRECONDITION_FAILED = "PRECONDITION_FAILED"
INTERNAL_VERIFICATION_FAILED = "INTERNAL_VERIFICATION_FAILED"
REQUEST_MALFORMED = "REQUEST_MALFORMED"
RUN_NOT_FOUND = "RUN_NOT_FOUND"
UNEXPECTED_ERROR = "UNEXPECTED_ERROR"


class PlannerError(Exception):
    """Base class for all contract errors."""

    category: str = COMPUTATION_FAILED
    code: str = UNEXPECTED_ERROR

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.details = details or []

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "code": self.code,
            "message": str(self),
            "details": self.details,
        }


class ValidationError(PlannerError):
    category = INPUT_ERROR
    code = LITERAL_MALFORMED


class StateConflictError(PlannerError):
    category = STATE_CONFLICT
    code = PRECONDITION_FAILED


class ResourceLimitError(PlannerError):
    category = RESOURCE_EXHAUSTED
    code = GROUNDING_LIMIT


class ComputationError(PlannerError):
    category = COMPUTATION_FAILED
    code = INTERNAL_VERIFICATION_FAILED


class IssueList:
    """Collector that lets validation report every problem at once."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(
        self,
        code: str,
        source: str,
        message: str,
        *,
        value: Any = None,
    ) -> None:
        issue = {"code": code, "source": source, "message": message}
        if value is not None:
            issue["value"] = _safe_value(value)
        self.items.append(issue)

    def __bool__(self) -> bool:
        return bool(self.items)

    def raise_if_any(self) -> None:
        if self.items:
            codes = sorted({i["code"] for i in self.items})
            raise ValidationError(
                f"validation failed with {len(self.items)} issue(s): "
                + ", ".join(codes),
                code=codes[0],
                details=self.items,
            )


def _safe_value(value: Any) -> Any:
    try:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)
    except Exception:  # pragma: no cover - defensive
        return repr(value)
