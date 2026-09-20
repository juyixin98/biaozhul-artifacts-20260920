from __future__ import annotations


class CivicLedgerError(Exception):
    """Base class for expected, client-facing errors."""

    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, details: list | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or []


class ValidationError(CivicLedgerError):
    status_code = 422
    code = "validation_error"


class NotFoundError(CivicLedgerError):
    status_code = 404
    code = "not_found"


class ConflictError(CivicLedgerError):
    status_code = 409
    code = "conflict"


class IdempotencyConflictError(CivicLedgerError):
    status_code = 409
    code = "idempotency_conflict"


class PermissionError(CivicLedgerError):  # noqa: A001 - domain naming intentional
    status_code = 403
    code = "forbidden"


class AuthError(CivicLedgerError):
    status_code = 401
    code = "unauthorized"
