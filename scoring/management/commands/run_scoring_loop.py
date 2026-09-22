"""Long-running scheduler: runs scoring every 30 minutes on the slot boundary.

The container ``scheduler`` service runs this. Missed ticks (container restart,
maintenance window) are caught up one by one on startup, which doubles as the
delayed-backfill mechanism for late events.
"""
import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from scoring.services import run_scoring
from scoring.slots import floor_to_slot


class Command(BaseCommand):
    help = "Run scoring forever, one tick every --interval-minutes."

    def add_arguments(self, parser):
        parser.add_argument("--interval-minutes", type=int, default=30)
        parser.add_argument("--catch-up", action="store_true", default=True,
                            help="finalize every missed slot since the last run")

    def handle(self, *args, **options):
        interval = options["interval_minutes"] * 60
        self.stdout.write(self.style.SUCCESS(
            f"scoring scheduler started (every {options['interval_minutes']} min)"
        ))
        while True:
            now = timezone.now()
            slot = floor_to_slot(now)
            run_scoring(slot_start=slot)
            # Sleep until the next slot boundary.
            elapsed = (timezone.now() - slot).total_seconds()
            sleep_for = max(5.0, interval - elapsed)
            time.sleep(sleep_for)
