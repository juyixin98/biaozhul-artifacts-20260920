"""FastAPI application factory and error mapping.

Error contract: every failure returns a non-2xx status with a body of the
shape {"error": {"category": ..., "message": ...}}. Mining runs that fail
are persisted with status FAILED; they are never reported as success.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import APP_VERSION
from app.api.routes import NotFoundError, router
from app.config import Settings, load_settings
from app.corpus.store import Store
from app.logging_setup import get_logger, setup_logging
from app.mining.constraints import MiningConstraintError


def _error(status: int, category: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"category": category, "message": message}},
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    setup_logging(settings.log_level)
    logger = get_logger("api")

    store = Store(settings.db_path)
    store.init_schema()

    app = FastAPI(title="fspm-service", version=APP_VERSION)
    app.state.settings = settings
    app.state.store = store
    app.state.logger = logger

    @app.exception_handler(NotFoundError)
    async def not_found_handler(_: Request, exc: NotFoundError) -> JSONResponse:
        return _error(404, "not_found", str(exc))

    @app.exception_handler(RequestValidationError)
    async def validation_handler(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error(422, "validation_error", str(exc.errors()))

    @app.exception_handler(MiningConstraintError)
    async def constraint_handler(
        _: Request, exc: MiningConstraintError
    ) -> JSONResponse:
        return _error(422, "constraint_error", str(exc))

    @app.exception_handler(Exception)
    async def internal_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_error", exc_info=exc)
        return _error(500, "internal", f"{type(exc).__name__}: {exc}")

    app.include_router(router)
    return app


app = create_app()
