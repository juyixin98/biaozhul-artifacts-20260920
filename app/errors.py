"""Domain error taxonomy.

Failures are categorized explicitly instead of being collapsed into a generic
"success" response.  Every error carries a stable ``code`` that the HTTP layer
maps to a distinct status code, so tests can assert both the failure category
and the response shape.
"""
from __future__ import annotations


class SPDError(Exception):
    """Base class for all expected domain failures."""

    code = "spm_error"
    http_status = 400

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, "details": self.details}


class NotFoundError(SPDError):
    code = "not_found"
    http_status = 404


class ValidationFailure(SPDError):
    """Input failed corpus / query specification validation."""

    code = "validation_failure"
    http_status = 422


class EmptyCorpusError(ValidationFailure):
    code = "empty_corpus"


class InvalidEventError(ValidationFailure):
    code = "invalid_event"


class InvalidSequenceError(ValidationFailure):
    code = "invalid_sequence"


class InvalidConstraintError(ValidationFailure):
    code = "invalid_constraint"


class ConflictingCorpusError(SPDError):
    code = "corpus_conflict"
    http_status = 409


class MiningInternalError(SPDError):
    """An invariant of the mining kernel was violated (never expected)."""

    code = "mining_internal_error"
    http_status = 500
