import asyncio
import contextlib
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.errors import LeaseError
from app.lifecycle import run_migrations, seed, wait_for_database, _sweeper_loop
from app.routers import auth, device, platform, tenant

logger = logging.getLogger("cloudgate")


def create_app(*, auto_migrate: bool = True, auto_seed: bool = True) -> FastAPI:
    app = FastAPI(
        title="CloudGate Control Plane",
        version="1.0.0",
        description="Multi-tenant VPN access control simulation (no real VPN / host routes).",
    )

    @app.exception_handler(LeaseError)
    async def lease_error_handler(_: Request, exc: LeaseError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        return {
            "status": "ok",
            "heartbeat_interval_seconds": settings.heartbeat_interval_seconds,
            "heartbeat_timeout_seconds": settings.heartbeat_timeout_seconds,
        }

    app.include_router(auth.router)
    app.include_router(platform.router)
    app.include_router(tenant.router)
    app.include_router(device.router)

    @app.on_event("startup")
    async def _on_startup() -> None:
        if auto_migrate:
            wait_for_database()
            run_migrations()
        if auto_seed:
            seed()
        app.state.sweeper_stop = stop = asyncio.Event()
        app.state.sweeper_task = asyncio.create_task(_sweeper_loop(stop))
        logger.info("cloudgate control plane started")

    @app.on_event("shutdown")
    async def _on_shutdown() -> None:
        stop = getattr(app.state, "sweeper_stop", None)
        task = getattr(app.state, "sweeper_task", None)
        if stop is not None:
            stop.set()
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return app


app = create_app()
