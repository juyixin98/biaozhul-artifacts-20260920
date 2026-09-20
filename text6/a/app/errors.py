class ConsentVaultError(Exception):
    """Base error carrying an HTTP status and machine-readable code."""

    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class NotFound(ConsentVaultError):
    status_code = 404
    code = "not_found"


class Conflict(ConsentVaultError):
    status_code = 409
    code = "conflict"


class Unprocessable(ConsentVaultError):
    status_code = 422
    code = "unprocessable"


class Gone(ConsentVaultError):
    status_code = 410
    code = "gone"


class Forbidden(ConsentVaultError):
    status_code = 403
    code = "forbidden"


class Unauthorized(ConsentVaultError):
    status_code = 401
    code = "unauthorized"
