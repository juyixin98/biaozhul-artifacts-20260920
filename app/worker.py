"""In-process training worker.

One API process hosts a small fixed-size thread pool. At each tick it first
re-queues jobs whose leases expired (fencing their old executors), then lets
an idle thread claim a job and run it. Scaling to multiple processes is out
of scope; ``claim_job`` is nonetheless safe under concurrency (PostgreSQL
row locks + per-user advisory lock), and tests exercise two workers
claiming directly.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid

from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db import SessionLocal
from . import queue as q
from .runner import run_job

log = logging.getLogger("nnlab.worker")


class WorkerPool:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.executor_id = f"worker-{uuid.uuid4().hex[:12]}"

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        for i in range(max(1, self.settings.worker_threads)):
            t = threading.Thread(
                target=self._loop, name=f"nnlab-worker-{i}", daemon=True
            )
            t.start()
            self._threads.append(t)
        log.info("worker pool started: %d threads, executor %s",
                 len(self._threads), self.executor_id)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=timeout)
        self._threads = []

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("worker tick failed")
            self._stop.wait(self.settings.worker_poll_interval)

    def _tick(self) -> None:
        with SessionLocal() as db:
            q.reap_expired_leases(db)
            job = q.claim_job(db, executor_id=self.executor_id,
                              settings=self.settings)
            if job is None:
                return
            job_id = job.id
        log.info("claimed job %s", job_id)
        # Dedicated session for the whole training run.
        with SessionLocal() as run_db:
            from .models import Job
            job = run_db.get(Job, job_id)
            run_job(run_db, job, settings=self.settings)
        log.info("job %s released by worker", job_id)
