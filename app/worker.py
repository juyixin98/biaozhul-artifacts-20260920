"""In-process timeout escalation worker.

No message queue or scheduler is used: a single asyncio loop polls for due
tasks every ``ESCALATION_POLL_INTERVAL`` seconds.  Each batch runs in its own
transaction; a failure is logged and retried on the next tick, and after a
restart due-but-unprocessed tasks are simply picked up again because
escalation is guarded by row locks and the ``escalated`` flag.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from app.config import get_settings
from app.db import SessionLocal
from app.engine import process_due_escalations

logger = logging.getLogger("workflow.worker")


async def escalation_worker(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    logger.info(
        "escalation worker started (interval=%ss, batch=%s)",
        settings.escalation_poll_interval,
        settings.escalation_batch_limit,
    )
    while not stop_event.is_set():
        try:
            async with SessionLocal() as session:
                count = await process_due_escalations(
                    session, settings.escalation_batch_limit
                )
                await session.commit()
            if count:
                logger.info("escalated %s task(s)", count)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - worker must keep retrying
            logger.exception("escalation batch failed; will retry")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                stop_event.wait(), timeout=settings.escalation_poll_interval
            )
    logger.info("escalation worker stopped")
