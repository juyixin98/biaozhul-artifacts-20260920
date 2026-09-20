class DomainError(Exception):
    status_code = 400

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class NotFoundError(DomainError):
    status_code = 404


class ForbiddenError(DomainError):
    status_code = 403


class ConflictError(DomainError):
    status_code = 409


class ConstraintViolationError(DomainError):
    """No caregiver can take the task (or a manual adjustment breaks a rule).

    Carries the concrete unsatisfied constraints per caregiver so the caller
    can see *why* scheduling failed instead of forcing a bad assignment."""

    status_code = 422

    def __init__(self, detail: str, violations: list | None = None):
        super().__init__(detail)
        self.violations = violations or []
