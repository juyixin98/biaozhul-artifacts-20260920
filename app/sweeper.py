from __future__ import annotations

import asyncio
import contextlib
import logging

from sqlalchemy.orm import Session

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.services import sweep_expired

log = logging.getLogger("cloudgate.sweeper")


def run_sweep_once(db: Session) -> int:
    return sweep_expired(db)


async def sweeper_loop(stop: asyncio.Event) -> None:
    settings = get_settings()
    log.info("lease sweeper started (interval=%ss, ttl=%ss)", settings.sweeper_interval_seconds, settings.heartbeat_ttl_seconds)
    while not stop.is_set():
        try:
            db = SessionLocal()
            try:
                closed = sweep_expired(db)
                if closed:
                    log.info("swept %d expired lease(s)", closed)
            finally:
                db.close()
        except Exception:
            log.exception("sweeper iteration failed")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.sweeper_interval_seconds)
    log.info("lease sweeper stopped")


def recover_on_startup() -> int:
    """Close leases that expired while the service was down."""
    db = SessionLocal()
    try:
        return sweep_expired(db, batch_size=1000)
    finally:
        db.close()
