"""FastAPI application factory."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from app import __version__
from app.api.routes import router
from app.api.service import SolveService
from app.config import Settings, get_settings
from app.store.evidence import EvidenceStore


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    store = EvidenceStore(settings.database_path)
    app = FastAPI(title="Finite-domain CSP service", version=__version__)
    app.state.settings = settings
    app.state.evidence_store = store
    app.state.solve_service = SolveService(store)
    app.include_router(router, prefix="/api")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
