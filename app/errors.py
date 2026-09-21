from __future__ import annotations


class VaultError(Exception):
    """Domain error. `code` is machine-readable; `message` is safe to show.

    Messages must never contain private/API key material or ciphertext.
    """

    http_status: int = 400

    def __init__(self, code: str, message: str, http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        if http_status is not None:
            self.http_status = http_status


class ConflictError(VaultError):
    http_status = 409


class BadRequest(VaultError):
    http_status = 400


class LimitError(VaultError):
    http_status = 429


class NotFound(VaultError):
    http_status = 404


class Forbidden(VaultError):
    http_status = 403


class Unauthorized(VaultError):
    http_status = 401
