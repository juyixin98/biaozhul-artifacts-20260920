"""Full rebuild equivalence and concurrency with concurrent imports."""

import threading
import time
from unittest import mock

from django.test import TestCase, TransactionTestCase

from conformance import services
from conformance.models import Case, Event
from conformance.services import import_events, rebuild_project

from .base import ev, make_project, make_template


def snapshot(project):
    """Current analysis outcome of every case, keyed by case_key."""
    result = {}
    for case in Case.objects.filter(project=project):
        current = case.analyses.filter(is_current=True).first()
        result[case.case_key] = current.outcome() if current else None
    return result


class RebuildConsistencyTests(TestCase):
    """Incremental analysis and full rebuild must produce the same result."""

    def setUp(self):
        self.user, self.project = make_project()
        self.template, self.version = make_template(self.project)

    def test_incremental_equals_full_rebuild(self):
        v = self.version
        # messy arrival order across several batches
        import_events(
            self.project,
            [
                ev("a2", "C1", "b", "2026-09-01T00:30:00Z", 2, v),
                ev("b1", "C2", "a", "2026-09-01T00:00:00Z", 1, v),
                ev("a1", "C1", "a", "2026-09-01T00:00:00Z", 1, v),
            ],
        )
        import_events(
            self.project,
            [
                ev("a4", "C1", "e", "2026-09-01T02:00:00Z", 4, v),  # gap at 3
                ev("b2", "C2", "b", "2026-09-01T05:00:00Z", 2, v),  # timeout
                ev("b3", "C2", "c", "2026-09-01T06:00:00Z", 3, v),
                ev("b4", "C2", "d", "2026-09-01T06:30:00Z", 4, v),  # xor clash
            ],
        )
        import_events(
            self.project,
            [ev("a3", "C1", "c", "2026-09-01T01:00:00Z", 3, v)],  # late fill
        )

        before = snapshot(self.project)
        self.assertTrue(all(s is not None for s in before.values()))

        run = rebuild_project(self.project)
        self.assertEqual(run.cases_processed, 2)

        after = snapshot(self.project)
        self.assertEqual(before, after)

        # rebuild over unchanged data creates no new revisions
        revisions_before = {
            c.case_key: c.analyses.count()
            for c in Case.objects.filter(project=self.project)
        }
        rebuild_project(self.project)
        revisions_after = {
            c.case_key: c.analyses.count()
            for c in Case.objects.filter(project=self.project)
        }
        self.assertEqual(revisions_before, revisions_after)

        self.assertFalse(
            Case.objects.filter(project=self.project, needs_analysis=True).exists()
        )


class ConcurrentRebuildTests(TransactionTestCase):
    """Events imported during a full rebuild must not be lost."""

    def test_import_during_rebuild(self):
        user, project = make_project()
        template, version = make_template(project)
        v = version
        import_events(
            project,
            [
                ev("a1", "C1", "a", "2026-09-01T00:00:00Z", 1, v),
                ev("a2", "C1", "b", "2026-09-01T00:30:00Z", 2, v),
                ev("b1", "C2", "a", "2026-09-01T00:00:00Z", 1, v),
            ],
        )

        original_compute = services.compute_case_analysis

        def slow_compute(case):
            # widen the rebuild window so the import lands mid-rebuild
            time.sleep(0.3)
            return original_compute(case)

        errors = []

        def run_rebuild():
            try:
                with mock.patch.object(
                    services, "compute_case_analysis", side_effect=slow_compute
                ):
                    rebuild_project(project)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        worker = threading.Thread(target=run_rebuild)
        worker.start()
        time.sleep(0.1)  # let the rebuild get going

        # late events arriving while the rebuild is running
        import_events(
            project,
            [
                ev("a3", "C1", "c", "2026-09-01T01:00:00Z", 3, v),
                ev("b2", "C2", "b", "2026-09-01T00:30:00Z", 2, v),
            ],
        )
        worker.join(timeout=30)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

        # no event lost, every case analyzed against all of its events
        self.assertEqual(Event.objects.filter(project=project).count(), 5)
        for case in Case.objects.filter(project=project):
            current = case.analyses.get(is_current=True)
            self.assertEqual(
                current.event_count,
                case.events.count(),
                f"case {case.case_key} analysis is stale",
            )
            self.assertFalse(case.needs_analysis)

        # and the result equals a clean full rebuild
        before = snapshot(project)
        rebuild_project(project)
        self.assertEqual(before, snapshot(project))
