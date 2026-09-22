"""True two-device concurrency tests.

These use real threads with independent database connections and row locks,
which SQLite (file-level locking) cannot model -- they skip automatically
unless MySQL is the configured backend.
Run with the compose MySQL service:

    docker compose exec web python manage.py test sync.tests.test_concurrency
"""
import threading
import uuid

from django.db import connection
from django.test import TransactionTestCase, skipUnlessDBFeature

from accounts.models import Role
from forms.models import FormTemplate
from sync.models import FormRecord, RecordStatus
from sync.tests.factories import (
    V1_FIELDS,
    assign,
    make_project_crew,
    make_user,
)
from sync.services import submit_batch


def entry(u, template_id, crew_id, notes, record_version):
    return {
        "uuid": str(u),
        "template_id": template_id,
        "template_version": 1,
        "record_version": record_version,
        "crew_id": crew_id,
        "collected_at": "2026-09-20T08:00:00Z",
        "data": {
            "structure_id": "S-1",
            "surface": "steel",
            "crack_count": None,
            "inspection_date": "2026-09-20",
            "notes": notes,
        },
    }


@skipUnlessDBFeature("has_select_for_update")
class ConcurrentDeviceTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        from django.db import transaction

        self.project, self.crew = make_project_crew()
        # Two devices owned by two workers in the same crew.
        self.user_a = make_user("device_a", Role.WORKER)
        self.user_b = make_user("device_b", Role.WORKER)
        assign(self.user_a, self.project, self.crew)
        assign(self.user_b, self.project, self.crew)
        self.template = FormTemplate.objects.create(
            project=self.project, code="deck", name="Deck"
        )
        with transaction.atomic():
            self.v1 = self.template.publish_version(V1_FIELDS, published_by=None)

    def _run_in_thread(self, fn):
        errors = []

        def wrapper():
            try:
                fn()
            except Exception as exc:  # pragma: no cover - surfaced in assertion
                errors.append(exc)
            finally:
                connection.close()

        t = threading.Thread(target=wrapper)
        t.start()
        return t, errors

    def test_concurrent_creates_same_uuid_different_content_become_conflict(self):
        u = uuid.uuid4()
        barrier = threading.Barrier(2)
        results = {}

        def send_a():
            barrier.wait()
            _, outcomes, _ = submit_batch(
                self.user_a, "batch-a",
                [entry(u, self.template.id, self.crew.id, "from-A", 0)],
                can_submit=lambda user, crew_id: True,
            )
            results["a"] = outcomes[0]

        def send_b():
            barrier.wait()
            _, outcomes, _ = submit_batch(
                self.user_b, "batch-b",
                [entry(u, self.template.id, self.crew.id, "from-B", 0)],
                can_submit=lambda user, crew_id: True,
            )
            results["b"] = outcomes[0]

        ta, ea = self._run_in_thread(send_a)
        tb, eb = self._run_in_thread(send_b)
        ta.join()
        tb.join()
        self.assertEqual(ea, [])
        self.assertEqual(eb, [])

        record = FormRecord.objects.get(record_uuid=u)
        self.assertEqual(record.status, RecordStatus.CONFLICT)
        self.assertEqual(record.revisions.count(), 2)
        notes = set(record.revisions.values_list("data__notes", flat=True))
        self.assertEqual(notes, {"from-A", "from-B"})

        status_codes = {results["a"]["status_code"], results["b"]["status_code"]}
        # Exactly one side is the 201 creator; the other receives 409.
        self.assertEqual(status_codes, {201, 409})

    def test_concurrent_edits_same_base_both_versions_retained(self):
        # Baseline revision exists (both devices synced it earlier).
        u = uuid.uuid4()
        submit_batch(
            self.user_a, "base-batch",
            [entry(u, self.template.id, self.crew.id, "base", 0)],
            can_submit=lambda user, crew_id: True,
        )
        barrier = threading.Barrier(2)
        results = {}

        def edit_a():
            barrier.wait()
            _, outcomes, _ = submit_batch(
                self.user_a, "edit-a",
                [entry(u, self.template.id, self.crew.id, "edit-A", 1)],
                can_submit=lambda user, crew_id: True,
            )
            results["a"] = outcomes[0]

        def edit_b():
            barrier.wait()
            _, outcomes, _ = submit_batch(
                self.user_b, "edit-b",
                [entry(u, self.template.id, self.crew.id, "edit-B", 1)],
                can_submit=lambda user, crew_id: True,
            )
            results["b"] = outcomes[0]

        ta, ea = self._run_in_thread(edit_a)
        tb, eb = self._run_in_thread(edit_b)
        ta.join()
        tb.join()
        self.assertEqual(ea, [])
        self.assertEqual(eb, [])

        record = FormRecord.objects.get(record_uuid=u)
        self.assertEqual(record.status, RecordStatus.CONFLICT)
        self.assertEqual(record.revisions.count(), 3)
        notes = set(record.revisions.values_list("data__notes", flat=True))
        self.assertEqual(notes, {"base", "edit-A", "edit-B"})
        # The fast-forward winner's content is the current head; the loser is
        # the conflict sibling -- neither is lost.
        self.assertIn(record.current_revision.data["notes"], {"edit-A", "edit-B"})
        self.assertEqual({results["a"]["status_code"], results["b"]["status_code"]}, {200, 409})

    def test_concurrent_identical_retries_return_same_revision(self):
        u = uuid.uuid4()
        e = entry(u, self.template.id, self.crew.id, "same", 0)
        barrier = threading.Barrier(2)
        results = {}

        def send(user, batch_id, key):
            def run():
                barrier.wait()
                _, outcomes, _ = submit_batch(
                    user, batch_id, [dict(e)], can_submit=lambda *_: True
                )
                results[key] = outcomes[0]

            t, errs = self._run_in_thread(run)
            return t, errs

        ta, ea = send(self.user_a, "ra", "a")
        tb, eb = send(self.user_b, "rb", "b")
        ta.join()
        tb.join()
        self.assertEqual(ea, [])
        self.assertEqual(eb, [])

        record = FormRecord.objects.get(record_uuid=u)
        # Same UUID + same content submitted concurrently: only one revision
        # is ever stored; both callers receive the same accepted result.
        self.assertEqual(record.revisions.count(), 1)
        self.assertEqual(record.status, RecordStatus.OK)
        self.assertEqual(results["a"]["status_code"], 201)
        self.assertEqual(results["b"]["status_code"], 201)
        self.assertEqual(results["a"]["content_hash"], results["b"]["content_hash"])
