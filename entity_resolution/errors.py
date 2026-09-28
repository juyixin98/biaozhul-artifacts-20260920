"""Categorical error taxonomy.

Every layer raises (or maps to) one of these error categories so that the four
required failure classes are distinguishable end to end:

* INPUT_ERROR      - malformed payload / failed schema validation
* STATE_CONFLICT   - constraint contradiction, lock violation, stale version
* RESOURCE_EXHAUSTED - corpus too large to solve exhaustively, budget used
* COMPUTATION_FAILED - unexpected internal/algorithm failure

``error_code`` is the stable, machine-readable identifier used on the wire and
in the run journal; the HTTP status mapping lives in :mod:`entity_resolution.api`.
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    # Input validation
    INVALID_REQUEST = "INPUT_ERROR:INVALID_REQUEST"
    RECORD_NOT_FOUND = "INPUT_ERROR:RECORD_NOT_FOUND"
    EMPTY_CORPUS = "INPUT_ERROR:EMPTY_CORPUS"
    DUPLICATE_RECORD_ID = "INPUT_ERROR:DUPLICATE_RECORD_ID"

    # State / constraint conflicts
    CONSTRAINT_CONFLICT = "STATE_CONFLICT:CONSTRAINT_CONFLICT"
    LOCK_VIOLATION = "STATE_CONFLICT:LOCK_VIOLATION"
    VERSION_CONFLICT = "STATE_CONFLICT:VERSION_CONFLICT"
    NO_SUCH_CLUSTER = "STATE_CONFLICT:NO_SUCH_CLUSTER"

    # Resource exhaustion
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED:BUDGET_OR_SIZE"

    # Internal
    COMPUTATION_FAILED = "COMPUTATION_FAILED:INTERNAL"


# Map each code to a category prefix (derived rather than duplicated).
CATEGORY_BY_CODE = {code: code.value.split(":", 1)[0] for code in ErrorCode}


class EntityResolutionError(Exception):
    """Base class for all domain errors.

    Attributes
    ----------
    code:
        Stable machine-readable error code.
    message:
        Human-readable, user-safe message (no secrets / internal traces).
    details:
        Structured context (record ids, offending pair, etc.).
    """

    code: ErrorCode = ErrorCode.COMPUTATION_FAILED

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict = details or {}

    @property
    def category(self) -> str:
        return CATEGORY_BY_CODE[self.code]

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "code": self.code.value,
            "message": self.message,
            "details": self.details,
        }


class InvalidRequestError(EntityResolutionError):
    code = ErrorCode.INVALID_REQUEST


class RecordNotFoundError(EntityResolutionError):
    code = ErrorCode.RECORD_NOT_FOUND


class EmptyCorpusError(EntityResolutionError):
    code = ErrorCode.EMPTY_CORPUS


class DuplicateRecordIdError(EntityResolutionError):
    code = ErrorCode.DUPLICATE_RECORD_ID


class ConstraintConflictError(EntityResolutionError):
    """Raised when a must-link and cannot-link contradict each other.

    ``details`` always carries ``pair`` and ``reason`` so callers (and the
    journal) can replay *why* a constraint set was rejected.
    """

    code = ErrorCode.CONSTRAINT_CONFLICT


class LockViolationError(EntityResolutionError):
    code = ErrorCode.LOCK_VIOLATION


class VersionConflictError(EntityResolutionError):
    code = ErrorCode.VERSION_CONFLICT


class NoSuchClusterError(EntityResolutionError):
    code = ErrorCode.NO_SUCH_CLUSTER


class ResourceExhaustedError(EntityResolutionError):
    code = ErrorCode.RESOURCE_EXHAUSTED


class ComputationFailedError(EntityResolutionError):
    code = ErrorCode.COMPUTATION_FAILED
