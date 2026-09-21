class SkillPulseError(Exception):
    """Base class for expected business errors mapped to HTTP responses."""

    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, code: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code


class NotFoundError(SkillPulseError):
    status_code = 404
    code = "not_found"


class ValidationError(SkillPulseError):
    status_code = 422
    code = "validation_error"


class ConflictError(SkillPulseError):
    status_code = 409
    code = "conflict"


class AuthError(SkillPulseError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(SkillPulseError):
    status_code = 403
    code = "forbidden"
