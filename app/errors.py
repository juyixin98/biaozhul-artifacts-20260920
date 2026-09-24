"""结构化错误码与异常。

所有对外错误均返回统一 JSON：
    {"error": {"code": "...", "message": "...", "details": {...}}}
"""

from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    """业务错误基类。"""

    http_status: int = 400
    code: str = "BAD_REQUEST"

    def __init__(self, message: str = "", details: dict[str, Any] | None = None):
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details or {}


class NotFound(ServiceError):
    http_status = 404
    code = "NOT_FOUND"


class BadRequest(ServiceError):
    http_status = 400
    code = "BAD_REQUEST"


class Conflict(ServiceError):
    """409：版本陈旧或时空冲突（携带冲突证据）。"""

    http_status = 409
    code = "CONFLICT"


class Forbidden(ServiceError):
    http_status = 403
    code = "FORBIDDEN"
