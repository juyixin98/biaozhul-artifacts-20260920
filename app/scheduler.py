"""In-process escalation scheduler.

A single daemon thread periodically claims due ``scheduled_escalations`` rows
with ``FOR UPDATE SKIP LOCKED``. Each job runs in its own transaction, so a
failing job is rolled back (still ``done = false``) and retried on the next
sweep; a service restart simply resumes from the durable table. No message
queue or external service is involved.
"""
from __future__ import annotations

import logging
import threading
import time

from sqlalchemy.exc import SQLAlchemyError

from .config import get_settings
from .db import SessionLocal
from .engine import due_escalations, fire_escalation

logger = logging.getLogger("wf.scheduler")


class EscalationScheduler:
    def __init__(self, interval_seconds: float | None = None):
        settings = get_settings()
        self.interval = interval_seconds or settings.scheduler_interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="escalation-scheduler", daemon=True)
        self._thread.start()
        logger.info("escalation scheduler started (interval=%ss)", self.interval)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None

    def tick(self) -> int:
        """Run one sweep; exposed for tests. Returns processed job count."""
        # Short read transaction: claim candidates with SKIP LOCKED, collect
        # their ids, then release before the per-job processing transactions.
        db = SessionLocal()
        try:
            job_ids = [job.id for job in due_escalations(db)]
            db.commit()
        finally:
            db.close()

        for job_id in job_ids:
            self._run_one(job_id)
        return len(job_ids)

    def _run_one(self, job_id) -> None:
        # Fresh session per job: one failure never poisons the rest.
        db = SessionLocal()
        try:
            from .models import ScheduledEscalation

            job = db.get(ScheduledEscalation, job_id, with_for_update=True)
            if job is None or job.done:
                db.rollback()
                return
            outcome = fire_escalation(db, job)
            db.commit()
            logger.info("escalation %s -> %s", job_id, outcome)
        except Exception:  # noqa: BLE001 - logged and retried next sweep
            db.rollback()
            logger.exception("escalation %s failed; will retry", job_id)
        finally:
            db.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except SQLAlchemyError:
                logger.exception("scheduler sweep failed; retrying")
            except Exception:  # noqa: BLE001 - the thread must never die
                logger.exception("unexpected scheduler error")
            self._stop.wait(self.interval)


scheduler = EscalationScheduler()
