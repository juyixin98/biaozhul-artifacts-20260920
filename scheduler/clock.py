"""Injectable clock.

Production code uses the real wall clock; tests install a ``FakeClock`` by
patching ``django.utils.timezone.now`` (see ``scheduler.tests.base``). Because
``Clock.now`` reads through ``django.utils.timezone`` and Django's
``auto_now``/``auto_now_add`` fields do the same, a single patch controls
*everything* — including ``enqueued_at`` and preemption ``created_at`` —
which is what makes queue timeouts, heartbeat staleness and the preemption
grace period deterministic in tests and in worker threads.
"""
from datetime import timedelta, timezone

from django.utils import timezone as dj_timezone


class Clock:
    def now(self):
        """Timezone-aware current time (overridable via timezone.now patch)."""
        return dj_timezone.now()


class FakeClock:
    def __init__(self, start=None):
        if start is None:
            start = dj_timezone.now().replace(microsecond=0)
        self._now = start

    def now(self):
        return self._now

    def advance(self, seconds):
        self._now = self._now + timedelta(seconds=seconds)
        return self._now

    def set(self, value):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        self._now = value
