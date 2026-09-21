"""FastAPI application with an in-process worker pool."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api.routes import router
from .db import init_db
from .workers.pool import WorkerPool


def create_app(start_workers: bool | None = None) -> FastAPI:
    if start_workers is None:
        start_workers = os.environ.get("ENABLE_WORKER", "1") != "0"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db()
        pool = None
        if start_workers:
            concurrency = int(os.environ.get("WORKER_CONCURRENCY", "2"))
            pool = WorkerPool(concurrency=concurrency)
            pool.start()
        app.state.pool = pool
        try:
            yield
        finally:
            if pool is not None:
                pool.stop()

    app = FastAPI(
        title="Neural Training Workflow",
        version="1.0.0",
        description=(
            "CPU-only Dense/ReLU/Dropout training with immutable "
            "architectures, leased queue, durable checkpoints and SSE metrics."
        ),
        lifespan=lifespan,
    )
    app.include_router(router, prefix="/api/v1")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


app = create_app()
