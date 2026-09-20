"""Batch event import: idempotency, conflict rejection, atomic rollback."""

from django.test import TestCase
from rest_framework.test import APIClient

from conformance.models import Event, ImportBatch
from conformance.services import BatchImportError, import_events

from .base import ev, make_project, make_template


class ImportTests(TestCase):
    def setUp(self):
        self.user, self.project = make_project()
        self.template, self.version = make_template(self.project)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def import_via_api(self, items):
        return self.client.post(
            f"/api/projects/{self.project.id}/events/import/",
            {"events": items},
            format="json",
        )

    def test_out_of_order_batch_accepted(self):
        resp = self.import_via_api(
            [
                ev("e2", "C1", "b", "2026-09-01T00:30:00Z", 2, self.version),
                ev("e1", "C1", "a", "2026-09-01T00:00:00Z", 1, self.version),
                ev("e3", "C1", "c", "2026-09-01T01:00:00Z", 3, self.version),
            ]
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["inserted"], 3)
        self.assertEqual(Event.objects.count(), 3)

    def test_duplicate_content_is_idempotent(self):
        items = [
            ev("e1", "C1", "a", "2026-09-01T00:00:00Z", 1, self.version),
            ev("e2", "C1", "b", "2026-09-01T00:30:00Z", 2, self.version),
        ]
        first = self.import_via_api(items)
        self.assertEqual(first.data["inserted"], 2)
        # re-import the exact same batch: skipped, not duplicated
        second = self.import_via_api(items)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.data["inserted"], 0)
        self.assertEqual(second.data["skipped"], 2)
        self.assertEqual(Event.objects.count(), 2)

    def test_same_id_different_content_rejected_and_rolled_back(self):
        self.import_via_api(
            [ev("e1", "C1", "a", "2026-09-01T00:00:00Z", 1, self.version)]
        )
        resp = self.import_via_api(
            [
                ev("e9", "C2", "a", "2026-09-01T00:00:00Z", 1, self.version),
                # same event_id, different activity -> conflict
                ev("e1", "C1", "b", "2026-09-01T00:00:00Z", 1, self.version),
            ]
        )
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(any("e1" in e for e in resp.data["errors"]))
        # whole batch rolled back: e9 must not exist either
        self.assertEqual(Event.objects.count(), 1)
        self.assertFalse(Event.objects.filter(event_id="e9").exists())
        batch = ImportBatch.objects.latest("id")
        self.assertEqual(batch.status, ImportBatch.Status.FAILED)

    def test_same_seq_different_event_rejected_and_rolled_back(self):
        self.import_via_api(
            [ev("e1", "C1", "a", "2026-09-01T00:00:00Z", 1, self.version)]
        )
        resp = self.import_via_api(
            [
                ev("e2", "C1", "b", "2026-09-01T00:30:00Z", 2, self.version),
                # seq 1 of case C1 already used by e1
                ev("e3", "C1", "b", "2026-09-01T00:35:00Z", 1, self.version),
            ]
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Event.objects.count(), 1)

    def test_conflicting_duplicates_within_batch_rejected(self):
        resp = self.import_via_api(
            [
                ev("e1", "C1", "a", "2026-09-01T00:00:00Z", 1, self.version),
                ev("e1", "C1", "b", "2026-09-01T00:05:00Z", 1, self.version),
            ]
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Event.objects.count(), 0)

    def test_new_case_requires_published_version(self):
        resp = self.import_via_api(
            [ev("e1", "C1", "a", "2026-09-01T00:00:00Z", 1)]
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Event.objects.count(), 0)

    def test_case_version_mismatch_rejected(self):
        from conformance.models import TemplateVersion
        from .base import FLOW_DEFINITION

        other = TemplateVersion.objects.create(
            template=self.template, version=2, definition=FLOW_DEFINITION,
            status=TemplateVersion.Status.PUBLISHED,
        )
        self.import_via_api(
            [ev("e1", "C1", "a", "2026-09-01T00:00:00Z", 1, self.version)]
        )
        resp = self.import_via_api(
            [ev("e2", "C1", "b", "2026-09-01T00:30:00Z", 2, other)]
        )
        self.assertEqual(resp.status_code, 400)

    def test_late_arrival_accepted(self):
        self.import_via_api(
            [
                ev("e1", "C1", "a", "2026-09-01T00:00:00Z", 1, self.version),
                ev("e3", "C1", "c", "2026-09-01T01:00:00Z", 3, self.version),
            ]
        )
        # late arrival of seq 2 is fine
        resp = self.import_via_api(
            [ev("e2", "C1", "b", "2026-09-01T00:30:00Z", 2, self.version)]
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(Event.objects.count(), 3)

    def test_service_raises_on_invalid_batch(self):
        with self.assertRaises(BatchImportError):
            import_events(self.project, [{"event_id": "x"}])
        self.assertEqual(Event.objects.count(), 0)
