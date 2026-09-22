"""Shared test helpers: a globally installed fake clock and small factories.

The fake clock patches ``django.utils.timezone.now`` for the whole process
for the duration of a test. Django's ``auto_now*`` model fields read through
that symbol, and so does ``Clock`` — therefore model timestamps, heartbeat
freshness, the 2-hour queue timeout and the preemption grace period are all
controlled by one clock (including inside worker threads).
"""
from contextlib import contextmanager
from unittest import mock

from django.conf import settings
from django.utils import timezone as dj_timezone

from rest_framework.test import APITestCase, APITransactionTestCase

from scheduler.clock import FakeClock
from scheduler import services
from scheduler.models import Node, ResourcePool


@contextmanager
def override_scheduler(**kwargs):
    """Temporarily override SCHEDULER settings (timeouts etc.)."""
    original = settings.SCHEDULER
    settings.SCHEDULER = {**original, **kwargs}
    try:
        yield
    finally:
        settings.SCHEDULER = original


class _ClockMixin:
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        # Fixed, deterministic "now" (2026-01-01 12:00 UTC).
        self.clock.set(
            __import__("datetime").datetime(2026, 1, 1, 12, 0, 0).replace(
                tzinfo=__import__("datetime").timezone.utc
            )
        )
        self._tz_patch = mock.patch.object(
            dj_timezone, "now", lambda: self.clock.now()
        )
        self._tz_patch.start()

    def tearDown(self):
        self._tz_patch.stop()
        super().tearDown()

    def now(self):
        return self.clock.now()

    def advance(self, seconds):
        return self.clock.advance(seconds)

    # -- factories ---------------------------------------------------------

    def make_pool(self, name="pool", max_gpus=16, max_jobs=8):
        return services.create_pool(
            name=name,
            max_total_gpus=max_gpus,
            max_concurrent_jobs=max_jobs,
        )

    def make_node(self, pool=None, hostname="n1", gpus=8, mem=81920,
                  heartbeat=True):
        pool = pool or self.make_pool()
        node = services.register_node(pool, hostname, gpus, mem)
        if heartbeat:
            services.heartbeat(hostname, self.now())
        return pool, node

    def submit(self, pool, name="job", mn=1, mx=1, mem=1024, prio=5):
        return services.create_job(pool, name, mn, mx, mem, prio)

    def tick(self):
        from scheduler.engine import tick

        return tick(self.clock)

    def refresh(self, obj):
        obj.refresh_from_db()
        return obj

    def node(self, hostname):
        return Node.objects.get(hostname=hostname)

    def pool(self, name="pool"):
        return ResourcePool.objects.get(name=name)


class SchedulerTestBase(_ClockMixin, APITestCase):
    """Wraps each test (and its factories) in an atomic transaction."""


class SchedulerTransactionBase(_ClockMixin, APITransactionTestCase):
    """Base for tests that spawn threads: rows must be committed for other
    DB connections (and therefore other threads) to see them."""

    reset_sequences = True
