"""Domain errors mapped to HTTP responses in ``app.main``."""
from __future__ import annotations


class ConsentError(Exception):
    status_code = 400
    code = "bad_request"


class NotFound(ConsentError):
    status_code = 404
    code = "not_found"


class Conflict(ConsentError):
    """409 — stale optimistic version or idempotency-key content mismatch."""

    status_code = 409
    code = "conflict"


class Unprocessable(ConsentError):
    """422 — semantically invalid request (e.g. grant without any policy)."""

    status_code = 422
    code = "unprocessable"
