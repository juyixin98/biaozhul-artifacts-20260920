"""统一错误类型与错误信封。"""

from __future__ import annotations


class ServiceError(Exception):
    """业务错误基类；映射为统一 JSON 错误信封。"""

    status_code: int = 400
    error_code: str = "bad_request"

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationError(ServiceError):
    status_code = 422
    error_code = "validation_error"


class PreprocessError(ServiceError):
    status_code = 422
    error_code = "preprocess_error"


class AuthError(ServiceError):
    status_code = 401
    error_code = "unauthorized"


def error_envelope(code: str, message: str, details: dict | None = None) -> dict:
    return {"ok": False, "error": {"code": code, "message": message, "details": details or {}}}
