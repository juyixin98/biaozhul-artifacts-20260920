"""Service-level errors mapped to HTTP responses in the API layer."""
from __future__ import annotations


class ServiceError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, detail: str, *, code: str | None = None):
        super().__init__(detail)
        self.detail = detail
        if code:
            self.code = code


class NotFoundError(ServiceError):
    status_code = 404
    code = "not_found"


class GoneError(ServiceError):
    status_code = 410
    code = "gone"


class ConflictError(ServiceError):
    status_code = 409
    code = "conflict"


class SemanticError(ServiceError):
    status_code = 422
    code = "semantic_error"
