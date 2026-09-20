from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI

from app.config import get_settings
from app.routers import admin, devices
from app.sweeper import recover_on_startup, sweeper_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("cloudgate")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="CloudGate",
        version="1.0.0",
        description="Multi-tenant access control plane (simulation; no real VPN or host routes).",
    )
    app.include_router(admin.router)
    app.include_router(devices.router)

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "cloudgate"}

    @app.on_event("startup")
    async def _startup() -> None:
        # Persistent leases survive restart; only past-deadline ones are reaped.
        try:
            reaped = recover_on_startup()
            if reaped:
                log.info("startup recovery closed %d stale lease(s)", reaped)
        except Exception:
            log.exception("startup recovery failed")

        app.state.sweeper_stop = asyncio.Event()
        if settings.run_sweeper:
            app.state.sweeper_task = asyncio.create_task(sweeper_loop(app.state.sweeper_stop))

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        stop = getattr(app.state, "sweeper_stop", None)
        if stop is not None:
            stop.set()
        task = getattr(app.state, "sweeper_task", None)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    return app


app = create_app()
