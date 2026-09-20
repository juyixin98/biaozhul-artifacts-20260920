import asyncio
import contextlib
import logging
import time

from sqlalchemy import text

from app import bootstrap
from app.config import settings
from app.db import SessionLocal, engine
from app.leasing import sweep_expired_sessions


def wait_for_database(max_attempts: int = 60, delay: float = 1.0) -> None:
    for attempt in range(1, max_attempts + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("select 1"))
            return
        except Exception as exc:  # noqa: BLE001 - postgres may still be starting
            if attempt == max_attempts:
                raise
            time.sleep(delay)


def run_migrations() -> None:
    """Apply Alembic migrations programmatically on boot."""
    from alembic import command
    from alembic.config import Config

    config = Config("alembic.ini")
    config.set_main_option("script_location", "migrations")
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "head")


def seed() -> None:
    with SessionLocal() as db:
        bootstrap.seed_platform_admin(db)
        bootstrap.seed_demo(db)


async def _sweeper_loop(stop: asyncio.Event) -> None:
    """Periodically release expired leases. Also the restart-recovery path:
    on boot the first sweep runs immediately, so leases left stale by a crash
    are reclaimed without waiting a full interval."""
    while not stop.is_set():
        try:
            with SessionLocal() as db:
                sweep_expired_sessions(db, timeout_seconds=settings.heartbeat_timeout_seconds)
        except Exception:  # noqa: BLE001 - sweeper must never kill the process
            logging.getLogger("cloudgate.sweeper").exception("lease sweep failed")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.sweep_interval_seconds)
