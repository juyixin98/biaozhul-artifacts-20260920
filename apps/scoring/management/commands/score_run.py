"""Score one period (latest closed by default) and exit.

Examples
--------
python manage.py score_run                 # latest closed period, idempotent
python manage.py score_run --force         # recompute even if complete
python manage.py score_run --period 2026-09-22T12:00:00Z
python manage.py score_run --backfill-from 2026-09-20T00:00:00Z
"""
from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from apps.scoring.services import (
    backfill,
    floor_to_period,
    run_latest,
    run_scoring,
)


class Command(BaseCommand):
    help = "Run the network scoring job for one 30-minute period (or backfill)."

    def add_arguments(self, parser):
        parser.add_argument("--period", help="ISO datetime of the period start")
        parser.add_argument(
            "--force", action="store_true", help="recompute a complete period"
        )
        parser.add_argument(
            "--backfill-from",
            dest="backfill_from",
            help="Recompute every closed period from this ISO datetime up to now",
        )

    def handle(self, *args, **options):
        if options["backfill_from"]:
            start = parse_datetime(options["backfill_from"])
            if start is None:
                raise CommandError("Invalid --backfill-from datetime")
            runs = backfill(from_time=start, force=True)
            self.stdout.write(
                f"Backfilled {len(runs)} period(s): "
                f"{runs[0].period_start if runs else '-'} .. "
                f"{runs[-1].period_start if runs else '-'}"
            )
            return

        if options["period"]:
            period = parse_datetime(options["period"])
            if period is None:
                raise CommandError("Invalid --period datetime")
            period = floor_to_period(period)
            run, created = run_scoring(period, force=options["force"])
        else:
            run, created = run_latest(force=options["force"])

        self.stdout.write(
            f"period={run.period_start.isoformat()} status={run.status} "
            f"touched={'yes' if created else 'no (already complete)'} "
            f"checksum={run.checksum[:12]}"
        )
