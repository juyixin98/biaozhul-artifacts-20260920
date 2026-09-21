"""Standalone worker process: run the queue loop without the web server.

Scale horizontally with docker compose (``--scale worker=N``); every process
claims jobs via ``SELECT ... FOR UPDATE SKIP LOCKED`` and renews its leases.
"""
from __future__ import annotations

import logging
import os
import signal
import time

from ..config import CHECKPOINT_DIR
from ..db import init_db
from .pool import WorkerPool


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    concurrency = int(os.environ.get("WORKER_CONCURRENCY", "2"))
    pool = WorkerPool(concurrency=concurrency, poll_interval=0.2)
    pool.start()

    stop = {"flag": False}

    def _handle(signum, frame):  # noqa: ANN001
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    logging.info("worker pool started (concurrency=%d)", concurrency)
    try:
        while not stop["flag"]:
            time.sleep(0.5)
    finally:
        pool.stop()
        pool.wait(timeout=10)
        logging.info("worker pool stopped")


if __name__ == "__main__":
    main()
