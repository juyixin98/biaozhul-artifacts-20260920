"""Domain error translated to HTTP errors at the API boundary."""
from __future__ import annotations


class DomainError(Exception):
    status_code = 400

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code


class NotFoundError(DomainError):
    status_code = 404


class ValidationError(DomainError):
    status_code = 422


class ConflictError(DomainError):
    status_code = 409


class AuthError(DomainError):
    status_code = 401


class ForbiddenError(DomainError):
    status_code = 403
