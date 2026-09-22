"""Domain exceptions shared by services (mapped to HTTP errors in views)."""


class DomainError(Exception):
    """Base class; ``status_code`` is the suggested HTTP status."""

    status_code = 400


class NotFoundError(DomainError):
    status_code = 404


class ConflictError(DomainError):
    status_code = 409


class ValidationError(DomainError):
    status_code = 400
