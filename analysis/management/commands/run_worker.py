"""Polling worker process: claim -> process -> heartbeat, forever.

Scale horizontally with docker compose --scale worker=N. Each process
generates its own worker identity per claim, so an OS-level kill only strands
a lease; reclaim_task picks it up after WORKER_LEASE_SECONDS.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from analysis import queue
from analysis.pipeline import process_task

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("analysis.worker")


class Command(BaseCommand):
    help = "Run a local analysis queue worker (Ctrl-C / SIGTERM to stop)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true",
                            help="Process at most one task and exit (tests/demo).")
        parser.add_argument("--idle-exit", type=int, default=0,
                            help="Exit after N consecutive idle polls (0 = never).")

    def handle(self, *args, **options):
        cfg = settings.QUEUE
        shutdown = threading.Event()

        def _graceful(signum, frame):
            logger.info("Signal %s received, stopping after current task", signum)
            shutdown.set()

        signal.signal(signal.SIGTERM, _graceful)
        signal.signal(signal.SIGINT, _graceful)

        idle_rounds = 0
        pid = os.getpid()
        logger.info("Worker pid=%s started lease=%ss poll=%.1fs",
                    pid, cfg["LEASE_SECONDS"], cfg["POLL_INTERVAL"])

        while not shutdown.is_set():
            task = queue.claim_task(cfg["LEASE_SECONDS"], cfg["MAX_ATTEMPTS"])
            if task is None:
                idle_rounds += 1
                if options["once"]:
                    logger.info("--once: queue empty, exiting")
                    return
                if options["idle_exit"] and idle_rounds >= options["idle_exit"]:
                    logger.info("Idle for %s rounds, exiting", idle_rounds)
                    return
                shutdown.wait(cfg["POLL_INTERVAL"])
                continue
            idle_rounds = 0
            logger.info("Claimed task=%s doc=%s gen=%s attempt=%s",
                        task.id, task.document_id, task.generation, task.attempts)

            stop_hb = threading.Event()

            def _heartbeat():
                # Heartbeat well inside the lease window.
                interval = min(cfg["HEARTBEAT_SECONDS"], cfg["LEASE_SECONDS"] / 3)
                while not stop_hb.wait(interval):
                    ok = queue.heartbeat(
                        task.id, task.worker_id, task.generation, cfg["LEASE_SECONDS"]
                    )
                    if not ok:
                        logger.warning(
                            "Heartbeat lost for task=%s (lease moved on); "
                            "continuing but result writes will be refused",
                            task.id,
                        )
                        return

            hb_thread = threading.Thread(target=_heartbeat, daemon=True)
            hb_thread.start()
            try:
                outcome = process_task(task)
            finally:
                stop_hb.set()
                hb_thread.join(timeout=5)
            logger.info("Task=%s outcome=%s", task.id, outcome)

            if options["once"]:
                return
