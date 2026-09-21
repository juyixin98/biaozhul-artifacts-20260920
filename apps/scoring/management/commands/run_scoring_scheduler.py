"""Long-running scheduler: trigger scoring every SCORING_PERIOD_MINUTES.

The loop is aligned to the wall clock (:00 and :30 UTC) rather than sleeping
a fixed 30 minutes from boot, so multiple replicas / restarts converge on
the same period boundaries. ``run_scoring`` is idempotent under the unique
(period_start) constraint, so an extra ticker never produces duplicates.

Used by the ``scheduler`` docker compose service.
"""
import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.scoring.services import latest_closed_period, run_scoring


class Command(BaseCommand):
    help = "Run network scoring every 30 minutes (aligned to :00/:30 UTC)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="score the latest closed period once and exit",
        )
        parser.add_argument(
            "--tick-seconds",
            type=int,
            default=30,
            help="how often to check whether a new period closed",
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS("Scoring scheduler started"))
        last_period = None
        if options["once"]:
            period = latest_closed_period()
            run, _ = run_scoring(period)
            self.stdout.write(f"Scored period {run.period_start}")
            return

        while True:
            period = latest_closed_period()
            if period != last_period:
                try:
                    run, created = run_scoring(period)
                    self.stdout.write(
                        f"[{timezone.now().isoformat()}] period="
                        f"{run.period_start.isoformat()} "
                        f"{'scored' if created else 'already complete'}"
                    )
                    last_period = period
                except Exception as exc:  # keep the scheduler alive
                    self.stderr.write(f"scoring failed for {period}: {exc}")
            time.sleep(options["tick_seconds"])
