"""Typed service-layer exceptions mapped to HTTP responses in the API layer."""
from __future__ import annotations

from typing import Any


class CareForceError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, details: Any | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFoundError(CareForceError):
    status_code = 404
    code = "not_found"


class AuthorizationError(CareForceError):
    status_code = 403
    code = "forbidden"


class ConflictError(CareForceError):
    status_code = 409
    code = "conflict"


class ValidationError(CareForceError):
    status_code = 422
    code = "validation_error"
