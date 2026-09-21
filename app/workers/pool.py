"""Background worker pool: poll the queue and execute claimed jobs."""
from __future__ import annotations

import logging
import threading
import uuid

from ..db import SessionLocal
from ..services import queue
from .runner import run_job

log = logging.getLogger("nnworkflow.worker")


class WorkerPool:
    def __init__(self, concurrency: int = 2, poll_interval: float = 0.2,
                 max_jobs: int | None = None):
        self.concurrency = concurrency
        self.poll_interval = poll_interval
        self.max_jobs = max_jobs  # stop whole pool after this many executions
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._executed = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        for _ in range(self.concurrency):
            t = threading.Thread(target=self._loop, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()

    def wait(self, timeout: float | None = None) -> None:
        for t in self._threads:
            t.join(timeout=timeout)

    def _budget_left(self) -> bool:
        if self.max_jobs is None:
            return True
        with self._lock:
            return self._executed < self.max_jobs

    def _loop(self) -> None:
        while not self._stop.is_set() and self._budget_left():
            executor_id = f"exec-{uuid.uuid4().hex}"
            db = SessionLocal()
            try:
                queue.requeue_expired_leases(db)
                job = queue.claim_next_job(db, executor_id)
            except Exception:  # noqa: BLE001
                log.exception("claim failed")
                job = None
            finally:
                db.close()

            if job is None:
                self._stop.wait(self.poll_interval)
                continue

            log.info("executor %s claimed job %s", executor_id, job.id)
            try:
                final = run_job(job.id, executor_id)
                log.info("job %s -> %s", job.id, final)
            except Exception:  # noqa: BLE001
                log.exception("job %s crashed", job.id)
            finally:
                with self._lock:
                    self._executed += 1
