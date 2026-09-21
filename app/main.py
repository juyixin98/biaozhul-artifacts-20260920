"""FastAPI application entrypoint and background invitation reaper."""
from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import Depends, FastAPI

from app.api import directory, internal, plans, tasks, worker
from app.clock import Clock
from app.config import get_settings
from app.db import SessionLocal
from app.deps import require_api_key
from app.services.allocation import expire_due_invitations

logger = logging.getLogger("careforce")

DESCRIPTION = """
CareForce — nursing-care task scheduling backend.

Scope: **task generation, constraint-based assignment, invitation expiry and
reassignment**. No payroll, volunteer or medical-decision features.

## Business rules

* Plans generate occurrences for the next **14 service-local days**, anchored
  to the plan timezone. Repeated generation never duplicates tasks (natural
  key: plan revision + template + service date).
* A plan revision only changes occurrences that have **not started**; started
  and completed tasks keep their old revision.
* Assignment enforces: qualifications valid over the whole interval, no time
  overlap, ≥10h rest between shifts, ≤44h per ISO week (overnight shifts are
  split at the UTC week boundary).
* Candidates rank by remaining weekly availability, then existing load, then
  worker id. If nobody fits, the concrete unsatisfied constraints are
  returned — nobody is force-scheduled.
* Invitations expire after **8 minutes** and are automatically reallocated.
  Concurrent accepts collapse onto exactly one assignment; repeat accepts
  are idempotent and never double-count hours.
* Coordinators may only schedule their **authorized units**. Manual changes
  run through the same constraint checks and are audited with a reason.

Identity is passed with `X-Coordinator-Id` / `X-Worker-Id` headers
(demo-grade; set `CAREFORCE_API_KEY` to additionally require an API key).
"""


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="CareForce Scheduling API",
        version="1.0.0",
        description=DESCRIPTION,
        dependencies=[Depends(require_api_key)],
    )
    app.state.settings = settings
    app.state.clock = Clock()

    app.include_router(directory.router)
    app.include_router(plans.router)
    app.include_router(tasks.router)
    app.include_router(worker.router)
    app.include_router(internal.router)

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        return {"status": "ok", "now": app.state.clock.now().isoformat()}

    @app.on_event("startup")
    async def _start_reaper() -> None:
        if settings.reaper_interval_seconds <= 0:
            return
        app.state.reaper_task = asyncio.create_task(_reaper_loop(app, settings.reaper_interval_seconds))

    @app.on_event("shutdown")
    async def _stop_reaper() -> None:
        task = getattr(app.state, "reaper_task", None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return app


async def _reaper_loop(app: FastAPI, interval_seconds: float) -> None:
    """Periodically expire due invitations and immediately reallocate."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await asyncio.to_thread(_run_reaper, app)
        except Exception:  # pragma: no cover - never let the loop die
            logger.exception("invitation reaper tick failed")


def _run_reaper(app: FastAPI) -> None:
    settings = app.state.settings
    clock: Clock = app.state.clock
    with SessionLocal() as session:
        try:
            expire_due_invitations(session, now=clock.now(), settings=settings)
            session.commit()
        except Exception:
            session.rollback()
            raise


app = create_app()
