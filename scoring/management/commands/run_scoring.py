"""Run a single scoring tick manually.

Usage::

    python manage.py run_scoring                 # tick for the current slot
    python manage.py run_scoring --at 2026-09-22T10:30:00Z
    python manage.py run_scoring --backfill '2026-09-20T00:00:00Z' \\
        --step-minutes 30                        # replay a range of slots

Re-running the same slot is idempotent and returns the stored run.
"""
from datetime import datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from scoring.services import run_scoring
from scoring.slots import floor_to_slot


def _parse(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise CommandError(f"invalid datetime: {value!r}") from exc


class Command(BaseCommand):
    help = "Run the 30-minute network scoring tick (idempotent per slot)."

    def add_arguments(self, parser):
        parser.add_argument("--at", type=str, default=None, help="ISO datetime")
        parser.add_argument("--backfill", type=str, default=None,
                            help="ISO datetime; replay every slot from this time up to --at/now")
        parser.add_argument("--step-minutes", type=int, default=30)

    def handle(self, *args, **options):
        at = _parse(options["at"]) if options["at"] else timezone.now()
        backfill = _parse(options["backfill"]) if options["backfill"] else None
        step = timedelta(minutes=options["step_minutes"])

        if backfill is None:
            run = run_scoring(slot_start=at)
            self._report(run)
            return

        backfill = floor_to_slot(backfill)
        end = floor_to_slot(at)
        if backfill > end:
            raise CommandError("--backfill must be earlier than --at/now")
        cursor = backfill
        count = 0
        while cursor <= end:
            run = run_scoring(slot_start=cursor)
            count += 1
            self._report(run)
            cursor += step
        self.stdout.write(self.style.SUCCESS(f"finalized {count} slot(s)"))

    def _report(self, run):
        self.stdout.write(
            f"slot={run.slot_start:%Y-%m-%dT%H:%M}Z "
            f"window=[{run.window_start:%Y-%m-%dT%H:%M},"
            f"{run.window_end:%Y-%m-%dT%H:%M}) "
            f"items={run.items.count()} hash={run.items_hash[:12]}"
        )
