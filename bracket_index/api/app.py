"""FastAPI application factory."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from ..config import Settings, load_settings
from ..diagnostics import configure_logging
from ..index.engine import IndexEngine
from ..index.errors import IndexError_
from ..index.store import Store
from .routes import build_router
from .validation import domain_error_handler

REQUEST_ID_HEADER = "X-Request-ID"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level)
    store = Store(settings.db_path)
    engine = IndexEngine(store, chunk_size=settings.chunk_size)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            store.close()

    app = FastAPI(title="bracket-index", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.store = store

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    app.add_exception_handler(IndexError_, domain_error_handler)
    app.include_router(build_router(engine))
    return app


app = create_app()
