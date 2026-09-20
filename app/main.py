from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import select

from .config import Settings, get_settings
from .database import make_engine, make_session_factory
from .models import AdminUser
from .routers import admin as admin_router
from .routers import device as device_router
from .security import hash_password
from .services.leases import run_sweep

logger = logging.getLogger("cloudgate")


def bootstrap_admin(session_factory, settings: Settings) -> None:
    """Create the initial global admin if it does not exist yet."""
    with session_factory() as db:
        exists = db.execute(
            select(AdminUser).where(AdminUser.username == settings.admin_username)
        ).scalar_one_or_none()
        if exists is None:
            db.add(
                AdminUser(
                    username=settings.admin_username,
                    password_hash=hash_password(settings.admin_password),
                    tenant_id=None,
                )
            )
            db.commit()
            logger.info("bootstrapped global admin %r", settings.admin_username)


async def _sweeper_loop(app: FastAPI) -> None:
    interval = app.state.settings.sweep_interval_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            released = await asyncio.to_thread(run_sweep, app.state.SessionLocal)
            if released:
                logger.info("sweeper released %d expired lease(s)", released)
        except Exception:
            logger.exception("sweeper iteration failed")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(bootstrap_admin, session_factory, settings)
        # Restart recovery: leases are durable, so on boot we immediately
        # release everything whose TTL elapsed while the service was down.
        await asyncio.to_thread(run_sweep, session_factory)
        sweeper = None
        if settings.sweep_interval_seconds > 0:
            sweeper = asyncio.create_task(_sweeper_loop(app))
        yield
        if sweeper is not None:
            sweeper.cancel()
        engine.dispose()

    app = FastAPI(title="CloudGate", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.SessionLocal = session_factory

    app.include_router(admin_router.router)
    app.include_router(device_router.router)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app


app = create_app()
