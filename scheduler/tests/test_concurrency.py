"""Concurrent scheduler tests.

These exercise multiple scheduler *processes/threads* ticking at once. On
MySQL the guarantee comes from ``SELECT ... FOR UPDATE`` row locks; on the
file-based SQLite DB used locally, serialized write transactions provide the
same externally visible property. We deliberately submit enough work that a
naive (unlocked) implementation would oversell.

They run on ``APITransactionTestCase`` because worker threads open their own
database connections and must be able to see committed rows (the normal
``TestCase`` wraps everything in one connection's uncommitted transaction).
"""
import threading
import time

from django.db import connection, transaction
from django.db.utils import OperationalError

from scheduler import services
from scheduler.clock import Clock
from scheduler.engine import tick
from scheduler.models import Allocation, Job, JobState, Node

from .base import SchedulerTransactionBase
from .test_scheduling import register


def _is_serialization_error(exc):
    """Lock/deadlock errors safe to retry as a whole transaction.

    SQLite raises 'database is locked' when two writers collide; MySQL
    surfaces 1213 (deadlock) / 1205 (lock wait timeout). Both mean 'run the
    whole tick again' — the engine tick is idempotent and re-reads
    authoritative state each round.
    """
    msg = str(exc).lower()
    return isinstance(exc, OperationalError) and any(
        s in msg for s in ("locked", "lock wait timeout", "deadlock")
    )


def _run_with_retries(fn, *, attempts=60, base_delay=0.02):
    for i in range(attempts):
        try:
            with transaction.atomic():
                return fn()
        except OperationalError as exc:
            connection.rollback()  # abort the failed txn before retrying
            if not _is_serialization_error(exc) or i == attempts - 1:
                raise
            time.sleep(base_delay * (1 + (i % 5)))


def _parallel(callables, *, barrier_count=None):
    """Run callables in threads; each thread closes only its OWN connection."""
    errors = []
    barrier = threading.Barrier(barrier_count or len(callables))

    def runner(fn):
        try:
            barrier.wait(timeout=15)
            _run_with_retries(fn)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            # Never close_all() — that would kill the main test connection.
            connection.close()

    threads = [threading.Thread(target=runner, args=(fn,)) for fn in callables]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        if t.is_alive():
            errors.append(RuntimeError("worker thread hung"))
    return errors


class ConcurrentSchedulerTests(SchedulerTransactionBase):
    def test_many_schedulers_never_oversell(self):
        pool = self.make_pool("p", max_gpus=8, max_jobs=32)
        register(self, pool, "n1", 4)
        register(self, pool, "n2", 4)
        for i in range(40):
            self.submit(pool, f"j{i}", mn=1, mx=1, prio=(i % 10) + 1)

        def work():
            for _ in range(3):
                tick(Clock())

        errors = _parallel_with_barrier([work] * 6)
        self.assertEqual(errors, [])

        running = Job.objects.filter(state=JobState.RUNNING).count()
        allocated = sum(n.allocated_gpus for n in Node.objects.filter(pool=pool))
        self.assertEqual(running, 8)
        self.assertEqual(allocated, 8)
        self.assertEqual(Allocation.objects.count(), 8)
        job_ids = list(Allocation.objects.values_list("job_id", flat=True))
        self.assertEqual(len(job_ids), len(set(job_ids)))
        for n in Node.objects.filter(pool=pool):
            self.assertLessEqual(n.allocated_gpus, n.gpu_count)

    def test_no_duplicate_allocation_under_concurrency(self):
        pool = self.make_pool("p2", max_gpus=8, max_jobs=8)
        register(self, pool, "n1", 8)
        for i in range(8):
            self.submit(pool, f"j{i}", mn=1, mx=1)

        def work():
            for _ in range(2):
                tick(Clock())

        errors = _parallel_with_barrier([work] * 8)
        self.assertEqual(errors, [])
        job_ids = list(Allocation.objects.values_list("job_id", flat=True))
        self.assertEqual(len(job_ids), len(set(job_ids)))
        self.assertEqual(len(job_ids), 8)

    def test_concurrent_completion_and_tick_settles_once(self):
        pool = self.make_pool("p3", max_gpus=4, max_jobs=8)
        register(self, pool, "n1", 4)
        jobs = [self.submit(pool, f"j{i}", mn=1, mx=1) for i in range(4)]
        self.tick()

        def complete_many():
            for j in jobs:
                services.report_completion(j, Clock().now())

        errors = _parallel_with_barrier(
            [complete_many, complete_many, lambda: tick(Clock())]
        )
        # Two completers race: one wins each CAS, errors list stays empty.
        self.assertEqual(errors, [])

        settle_count = pool.decisions.filter(kind="SETTLED").count()
        self.assertEqual(settle_count, 4)
        for j in jobs:
            j.refresh_from_db()
            self.assertEqual(j.state, JobState.COMPLETED)
        self.assertEqual(Node.objects.get(hostname="n1").allocated_gpus, 0)

    def test_concurrent_cancel_and_complete(self):
        pool = self.make_pool("p4", max_gpus=2, max_jobs=8)
        register(self, pool, "n1", 2)
        j1 = self.submit(pool, "j1", mn=1, mx=1)
        j2 = self.submit(pool, "j2", mn=1, mx=1)
        self.tick()

        fns = [
            lambda: services.report_completion(j1, Clock().now()),
            lambda: services.cancel_job(j1, Clock().now()),
            lambda: services.report_completion(j2, Clock().now()),
            lambda: services.cancel_job(j2, Clock().now()),
        ]
        errors = _parallel_with_barrier(fns)
        self.assertEqual(errors, [])
        for job in (j1, j2):
            job.refresh_from_db()
            self.assertIn(job.state, (JobState.COMPLETED, JobState.CANCELLED))
        self.assertEqual(Node.objects.get(hostname="n1").allocated_gpus, 0)
        self.assertEqual(pool.decisions.filter(kind="SETTLED").count(), 2)


def _parallel_with_barrier(callables):
    return _parallel(callables, barrier_count=len(callables))
