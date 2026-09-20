"""FastAPI application entrypoint.

Run locally:  uvicorn app.main:app --reload
Run in Docker: see docker-compose.yml.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.clock import SystemClock
from app.config import settings
from app.db import configure_engine
from app.routers import operations, plans, scheduling, tasks
from app.services.errors import CareForceError


def create_app() -> FastAPI:
    app = FastAPI(
        title="CareForce Scheduling API",
        version=__version__,
        description=(
            "Care-task generation, constrained assignment and timeout "
            "re-allocation for home-care operations."
        ),
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.exception_handler(CareForceError)
    async def careforce_error_handler(
        request: Request, exc: CareForceError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": exc.code,
                "message": exc.message,
                "details": exc.details,
            },
        )

    @app.on_event("startup")
    def _startup() -> None:
        configure_engine()

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/", tags=["meta"])
    def root() -> dict:
        return {
            "service": "careforce",
            "docs": "/docs",
            "openapi": "/openapi.json",
            "health": "/health",
        }

    app.include_router(plans.router)
    app.include_router(tasks.router)
    app.include_router(scheduling.router)
    app.include_router(operations.router)

    # Controllable clock; tests replace this with a FakeClock.
    app.state.clock = SystemClock()
    return app


app = create_app()
