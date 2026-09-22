"""Run the scheduling loop: periodically execute one engine tick.

Multiple copies of this process may run at once (HA / sharding). The
per-pool row lock taken inside ``engine.tick`` serializes them safely, so
GPUs are never oversold and jobs never double-allocated.
"""
import logging
import signal
import time

from django.core.management.base import BaseCommand
from django.db import connection

from scheduler.clock import Clock
from scheduler.engine import tick
from scheduler.services import recover_from_database

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the GPU scheduler loop forever."

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval",
            type=float,
            default=2.0,
            help="Seconds between scheduling ticks (default 2).",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run a single tick (after recovery) and exit.",
        )

    def handle(self, *args, **options):
        interval = options["interval"]
        once = options["once"]
        running = {"flag": True}

        def _stop(signum, frame):
            running["flag"] = False

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        clock = Clock()
        rec = recover_from_database(clock.now())
        self.stdout.write(
            self.style.SUCCESS(
                f"[run_scheduler] startup recovery: {rec}"
            )
        )

        while running["flag"]:
            started = time.monotonic()
            try:
                stats = tick(clock)
                self.stdout.write(
                    f"[tick {clock.now().isoformat()}] {stats}"
                )
            except Exception:  # noqa: BLE001 - loop must survive a failed tick
                logger.exception("scheduler tick failed")
            finally:
                # Do not hold idle DB connections open between rounds.
                connection.close()

            if once:
                break
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, interval - elapsed))

        self.stdout.write("[run_scheduler] stopped")
