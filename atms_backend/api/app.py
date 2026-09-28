"""FastAPI application factory and process entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ..config import settings
from ..core.budgets import Budgets
from .deps import init_service
from .routes import router


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def create_app(db_path: str | None = None) -> FastAPI:
    _configure_logging()
    chosen_db = db_path or settings.db_path
    budgets = Budgets(
        max_label_envs=settings.max_label_envs,
        max_total_envs=settings.max_total_envs,
        max_steps=settings.max_steps,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Open/initialize the SQLite store on startup, not at import time,
        # so importing the module (e.g. by the test suite) has no filesystem
        # side effects.
        app.state.service = init_service(chosen_db, budgets)
        yield

    app = FastAPI(
        title="ATMS Teaching Backend",
        version="0.1.0",
        description=(
            "Assumption-based truth maintenance: labels (supporting "
            "environments), nogoods and retraction scenarios."
        ),
        lifespan=lifespan,
    )
    app.include_router(router)
    return app


# uvicorn target: atms_backend.api.app:app (DB connects at startup).
app = create_app()


def main() -> None:  # pragma: no cover - process entrypoint
    import uvicorn

    uvicorn.run(
        "atms_backend.api.app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
