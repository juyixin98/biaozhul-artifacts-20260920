"""Replay analysis: gaps, predecessors, duplicates, XOR branches, timeouts."""

from django.test import TestCase

from conformance.models import Case, CaseAnalysis
from conformance.services import import_events

from .base import ev, make_project, make_template

T = "2026-09-01T00:00:00Z"


def at(hour, minute=0, day=1):
    return f"2026-09-{day:02d}T{hour:02d}:{minute:02d}:00Z"


class AnalysisTests(TestCase):
    def setUp(self):
        self.user, self.project = make_project()
        self.template, self.version = make_template(self.project)

    def current(self, case_key):
        case = Case.objects.get(project=self.project, case_key=case_key)
        return case.analyses.get(is_current=True)

    def import_items(self, items):
        return import_events(
            self.project, [ev(*item, version=self.version) for item in items]
        )

    def test_conformant_full_trace(self):
        self.import_items(
            [
                ("e1", "C1", "a", at(0), 1),
                ("e2", "C1", "b", at(0, 30), 2),
                ("e3", "C1", "c", at(1), 3),
                ("e4", "C1", "e", at(2), 4),
            ]
        )
        current = self.current("C1")
        self.assertEqual(current.status, CaseAnalysis.Status.CONFORMANT)
        self.assertEqual(current.deviations, [])
        self.assertEqual(current.missing_seqs, [])

    def test_gap_marks_waiting_and_never_conformant(self):
        self.import_items(
            [
                ("e1", "C1", "a", at(0), 1),
                ("e2", "C1", "b", at(0, 30), 2),
                # seq 3 missing
                ("e4", "C1", "e", at(2), 4),
            ]
        )
        current = self.current("C1")
        self.assertEqual(current.status, CaseAnalysis.Status.WAITING)
        self.assertEqual(current.missing_seqs, [3])
        self.assertNotEqual(current.status, CaseAnalysis.Status.CONFORMANT)

    def test_late_backfill_recomputes_and_keeps_revision_history(self):
        self.import_items(
            [
                ("e1", "C1", "a", at(0), 1),
                ("e2", "C1", "b", at(0, 30), 2),
                ("e4", "C1", "e", at(2), 4),
            ]
        )
        before = self.current("C1")
        self.assertEqual(before.status, CaseAnalysis.Status.WAITING)

        # late event fills the gap
        self.import_items([("e3", "C1", "c", at(1), 3)])
        after = self.current("C1")
        self.assertEqual(after.status, CaseAnalysis.Status.CONFORMANT)
        self.assertGreater(after.revision, before.revision)

        # old conclusion is preserved as a superseded revision
        case = Case.objects.get(project=self.project, case_key="C1")
        revisions = list(case.analyses.order_by("revision"))
        self.assertEqual(len(revisions), 2)
        self.assertEqual(revisions[0].status, CaseAnalysis.Status.WAITING)
        self.assertFalse(revisions[0].is_current)
        self.assertTrue(revisions[1].is_current)

    def test_missing_predecessor_with_evidence(self):
        self.import_items(
            [
                ("e1", "C1", "a", at(0), 1),
                # c requires b, which never ran
                ("e2", "C1", "c", at(1), 2),
            ]
        )
        current = self.current("C1")
        dev = [d for d in current.deviations if d["type"] == "missing_predecessor"]
        self.assertEqual(len(dev), 1)
        self.assertEqual(dev[0]["activity"], "c")
        self.assertEqual(dev[0]["constraint"]["predecessors"], ["b"])
        self.assertEqual(dev[0]["evidence"]["event"]["event_id"], "e2")

    def test_duplicate_execution_detected(self):
        self.import_items(
            [
                ("e1", "C1", "a", at(0), 1),
                ("e2", "C1", "b", at(0, 30), 2),
                ("e3", "C1", "b", at(0, 45), 3),
            ]
        )
        current = self.current("C1")
        dev = [d for d in current.deviations if d["type"] == "duplicate_execution"]
        self.assertEqual(len(dev), 1)
        self.assertEqual(dev[0]["activity"], "b")
        self.assertEqual(
            dev[0]["evidence"]["first_execution"]["event_id"], "e2"
        )
        self.assertEqual(
            dev[0]["evidence"]["duplicate_execution"]["event_id"], "e3"
        )

    def test_exclusive_branch_violation(self):
        self.import_items(
            [
                ("e1", "C1", "a", at(0), 1),
                ("e2", "C1", "b", at(0, 30), 2),
                ("e3", "C1", "c", at(1), 3),
                ("e4", "C1", "d", at(1, 30), 4),  # c and d are mutually exclusive
            ]
        )
        current = self.current("C1")
        dev = [d for d in current.deviations if d["type"] == "exclusive_violation"]
        self.assertEqual(len(dev), 1)
        self.assertEqual(dev[0]["constraint"]["exclusive_group"], ["c", "d"])
        self.assertEqual(
            dev[0]["evidence"]["first_execution"]["activity"], "c"
        )
        self.assertEqual(
            dev[0]["evidence"]["conflicting_execution"]["activity"], "d"
        )

    def test_single_branch_passes_join(self):
        """Taking the c-branch must not complain about the missing d."""
        self.import_items(
            [
                ("e1", "C1", "a", at(0), 1),
                ("e2", "C1", "b", at(0, 30), 2),
                ("e3", "C1", "c", at(1), 3),
                ("e4", "C1", "e", at(2), 4),
            ]
        )
        current = self.current("C1")
        self.assertEqual(current.deviations, [])
        self.assertEqual(current.status, CaseAnalysis.Status.CONFORMANT)

    def test_timeout_with_evidence_and_constraint(self):
        self.import_items(
            [
                ("e1", "C1", "a", at(0), 1),
                # 2 hours after a; limit is 1 hour
                ("e2", "C1", "b", at(2), 2),
            ]
        )
        current = self.current("C1")
        dev = [d for d in current.deviations if d["type"] == "timeout"]
        self.assertEqual(len(dev), 1)
        self.assertEqual(dev[0]["activity"], "b")
        self.assertEqual(dev[0]["constraint"]["max_seconds"], 3600)
        self.assertEqual(dev[0]["evidence"]["elapsed_seconds"], 7200)

    def test_unknown_activity_flagged(self):
        self.import_items(
            [
                ("e1", "C1", "a", at(0), 1),
                ("e2", "C1", "not-in-template", at(0, 30), 2),
            ]
        )
        current = self.current("C1")
        self.assertIn(
            "unknown_activity", [d["type"] for d in current.deviations]
        )

    def test_identical_recompute_creates_no_new_revision(self):
        self.import_items([("e1", "C1", "a", at(0), 1)])
        case = Case.objects.get(project=self.project, case_key="C1")
        from conformance.services import analyze_case

        analyze_case(case)
        self.assertEqual(case.analyses.count(), 1)
