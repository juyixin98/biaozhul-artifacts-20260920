"""FastAPI 应用工厂：装配设置、仓储、路由与统一错误契约。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.config import Settings
from app.errors import AppError, ErrorCategory
from app.store.repo import Repo

log = logging.getLogger("lexgen")

_STATUS_BY_CATEGORY = {
    ErrorCategory.INPUT_ERROR: 400,
    ErrorCategory.STATE_CONFLICT: 409,
    ErrorCategory.RESOURCE_EXHAUSTED: 413,
    ErrorCategory.COMPUTATION_FAILED: 500,
}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        app.state.repo.close()

    app = FastAPI(title="lexgen", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.repo = Repo(settings.db_path)

    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=_STATUS_BY_CATEGORY[exc.category],
            content=exc.to_payload(),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "category": ErrorCategory.INPUT_ERROR.value,
                    "code": "REQUEST_INVALID",
                    "message": "请求体结构校验失败",
                    "details": {"errors": errors},
                }
            },
        )

    @app.exception_handler(Exception)
    async def unknown_error_handler(_: Request, exc: Exception) -> JSONResponse:
        log.exception("未预期错误: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "category": ErrorCategory.COMPUTATION_FAILED.value,
                    "code": "INTERNAL",
                    "message": "内部计算失败",
                    "details": {"type": type(exc).__name__},
                }
            },
        )

    app.include_router(router)
    return app


app = create_app()
