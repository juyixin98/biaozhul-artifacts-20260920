"""业务错误类型。"""


class FlowError(Exception):
    status_code = 400

    def __init__(self, message: str, *, details: list | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or []


class ValidationError(FlowError):
    status_code = 422


class NotFoundError(FlowError):
    status_code = 404


class ConflictError(FlowError):
    """状态冲突 / 版本冲突 / 幂等指纹冲突。不写入任何业务数据。"""

    status_code = 409


class AuthzError(FlowError):
    status_code = 403
