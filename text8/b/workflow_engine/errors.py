"""领域错误与全局异常处理。"""

from fastapi import Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, extra: dict | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.extra = extra or {}


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class ValidationError(AppError):
    status_code = 422
    code = "definition_invalid"


class ReplayControl(Exception):
    """内部信号：命中幂等台账，直接回放原响应。绕过事务回滚。"""

    def __init__(self, payload: dict):
        self.payload = payload


def register_error_handlers(app) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        body = {"error": {"code": exc.code, "message": exc.message}}
        body["error"].update(exc.extra)
        return JSONResponse(status_code=exc.status_code, content=body)
