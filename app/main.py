"""FastAPI application entry point.

Run locally:

    uvicorn app.main:app --reload

All state is local (SQLite file + JSON-lines log configured in
config/settings.yaml). There are no external accounts or services.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from functools import lru_cache

from fastapi import FastAPI

from app.api.logging_setup import configure_logger
from app.api.routes import router
from app.config import CONFIG
from app.data.ledger import Ledger


@lru_cache(maxsize=1)
def get_logger() -> logging.Logger:
    return configure_logger(CONFIG.log_path, CONFIG.log_level)


@lru_cache(maxsize=1)
def get_ledger() -> Ledger:
    return Ledger(CONFIG.db_path)


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        get_logger().info(
            "service_started",
            extra={"core_version": CONFIG.core_version, "detail": str(CONFIG.db_path)},
        )
        yield
        get_logger().info("service_stopped", extra={"core_version": CONFIG.core_version})

    app = FastAPI(
        title="DID Panel Service",
        version=CONFIG.version,
        lifespan=lifespan,
        description=(
            "Two-group/two-period difference-in-differences and event-time "
            "descriptive synthesis for panel data, with identity alignment, "
            "fixed weights, unit-clustered inference and explicit diagnostics."
        ),
    )
    app.include_router(router)
    return app


app = create_app()
