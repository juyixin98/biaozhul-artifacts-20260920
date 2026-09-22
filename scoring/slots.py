"""30-minute slot alignment helpers (UTC)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.utils import timezone

SLOT = timedelta(minutes=settings.SCORING_INTERVAL_MINUTES)
WINDOW = timedelta(days=settings.SCORING_WINDOW_DAYS)

EPOCH = datetime(1970, 1, 1, tzinfo=dt_timezone.utc)


def floor_to_slot(moment: datetime) -> datetime:
    """Round ``moment`` down to the most recent 30-minute boundary."""
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, dt_timezone.utc)
    moment = moment.astimezone(dt_timezone.utc)
    delta = moment - EPOCH
    floored = (delta // SLOT) * SLOT
    return EPOCH + floored


def window_for_slot(slot_start: datetime) -> tuple[datetime, datetime, datetime]:
    """Return ``(slot_start, window_start, window_end)`` for a tick.

    Half-open window ``[slot_start - 7d, slot_start)`` -- events are scored
    exactly once per tick based on their ``occurred_at``.
    """
    slot_start = floor_to_slot(slot_start)
    return slot_start, slot_start - WINDOW, slot_start
