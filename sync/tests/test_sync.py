import uuid
from datetime import datetime

from django.test import TestCase

from accounts.models import CrewMembership, ProjectAssignment, Role
from forms.models import FormTemplate, Project
from sync.models import FormRecord, RecordRevision, RecordStatus
from sync.tests.factories import (
    V1_FIELDS,
    api_for,
    assign,
    make_project_crew,
    make_user,
    submit_entry,
)


def valid_data(**overrides):
    data = {
        "structure_id": "S-001",
        "surface": "steel",
        "crack_count": None,
        "inspection_date": "2026-09-20",
        "notes": "ok",
    }
    data.update(overrides)
    return data


class BatchSyncBase(TestCase):
    def setUp(self):
        self.project, self.crew = make_project_crew()
        self.worker = make_user("w1", Role.WORKER)
        assign(self.worker, self.project, self.crew)
        self.client = api_for(self.worker)

        self.template = FormTemplate.objects.create(
            project=self.project, code="deck", name="Deck"
        )
        self.v1 = self.template.publish_version(V1_FIELDS, published_by=None)

    def entry(self, data=None, record_version=0, record_uuid=None):
        return {
            "uuid": str(record_uuid or uuid.uuid4()),
            "template_id": self.template.id,
            "template_version": self.v1.version,
            "record_version": record_version,
            "crew_id": self.crew.id,
            "collected_at": "2026-09-20T08:30:00Z",
            "data": data or valid_data(),
        }


class BatchAcceptanceTests(BatchSyncBase):
    def test_create_then_idempotent_retry_same_content(self):
        e = self.entry()
        r1 = submit_entry(self.client, "batch-1", [e])
        self.assertEqual(r1.status_code, 200)
        row = r1.data["entries"][0]
        self.assertEqual(row["status_code"], 201)
        self.assertEqual(row["record_version"], 1)
        first_hash = row["content_hash"]

        # Retry: same UUID, same content (even a different batch id) must
        # replay the ORIGINAL result -- never a second revision.
        r2 = submit_entry(self.client, "batch-1", [e])
        self.assertTrue(r2.data["replayed"])
        row2 = r2.data["entries"][0]
        self.assertEqual(row2["status_code"], 201)
        self.assertEqual(row2["record_version"], 1)
        self.assertEqual(row2["content_hash"], first_hash)

        record = FormRecord.objects.get(record_uuid=e["uuid"])
        self.assertEqual(record.revisions.count(), 1)

    def test_key_order_and_whitespace_do_not_affect_hash(self):
        e = self.entry()
        submit_entry(self.client, "b1", [e])
        e2 = dict(e)
        e2["data"] = {
            "notes": "ok",
            "inspection_date": "2026-09-20",
            "crack_count": None,
            "surface": "steel",
            "structure_id": "S-001",
        }
        r = submit_entry(self.client, "b2", [e2])
        self.assertEqual(r.data["entries"][0]["status_code"], 201)
        self.assertEqual(
            FormRecord.objects.get(record_uuid=e["uuid"]).revisions.count(), 1
        )

    def test_different_content_at_same_version_conflicts_and_keeps_both(self):
        e = self.entry()
        submit_entry(self.client, "b1", [e])

        e2 = self.entry(record_uuid=e["uuid"], data=valid_data(notes="edited!"))
        r = submit_entry(self.client, "b2", [e2])
        row = r.data["entries"][0]
        self.assertEqual(row["status_code"], 409)
        self.assertIn("concurrent_modification", row["errors"]["__all__"])
        self.assertEqual(row["conflict_with_seq"], 1)

        record = FormRecord.objects.get(record_uuid=e["uuid"])
        self.assertEqual(record.status, RecordStatus.CONFLICT)
        # Both versions preserved, head not overwritten.
        seqs = list(record.revisions.values_list("seq", "data"))
        self.assertEqual(seqs[0][1]["notes"], "ok")
        self.assertEqual(seqs[1][1]["notes"], "edited!")
        self.assertEqual(record.current_revision.data["notes"], "ok")

    def test_legitimate_update_with_correct_record_version_is_fast_forward(self):
        e = self.entry()
        submit_entry(self.client, "b1", [e])
        # Client received record_version=1; edits against it.
        e2 = self.entry(
            record_uuid=e["uuid"], record_version=1, data=valid_data(notes="v2 edit")
        )
        r = submit_entry(self.client, "b2", [e2])
        row = r.data["entries"][0]
        self.assertEqual(row["status_code"], 200)
        self.assertEqual(row["record_version"], 2)
        record = FormRecord.objects.get(record_uuid=e["uuid"])
        self.assertEqual(record.status, RecordStatus.OK)
        self.assertEqual(record.current_revision.data["notes"], "v2 edit")

    def test_fast_forward_chain_then_stale_edit_conflicts(self):
        e = self.entry()
        submit_entry(self.client, "b1", [e])
        submit_entry(
            self.client,
            "b2",
            [self.entry(record_uuid=e["uuid"], record_version=1, data=valid_data(notes="v2"))],
        )
        # An offline device still on v1 commits a third edit.
        r = submit_entry(
            self.client,
            "b3",
            [self.entry(record_uuid=e["uuid"], record_version=1, data=valid_data(notes="stale"))],
        )
        self.assertEqual(r.data["entries"][0]["status_code"], 409)
        record = FormRecord.objects.get(record_uuid=e["uuid"])
        self.assertEqual(record.status, RecordStatus.CONFLICT)
        self.assertEqual(record.revisions.count(), 3)

    def test_rejected_validation_does_not_block_other_entries(self):
        good = self.entry()
        bad = self.entry(data=valid_data(surface="brick"))  # illegal enum
        missing = self.entry(data={"structure_id": "x"})
        r = submit_entry(self.client, "bmix", [good, bad, missing])
        codes = [row["status_code"] for row in r.data["entries"]]
        self.assertEqual(codes, [201, 422, 422])
        self.assertEqual(FormRecord.objects.count(), 1)

    def test_conditional_required_rejected_on_submit(self):
        e = self.entry(data=valid_data(surface="concrete", crack_count=None))
        r = submit_entry(self.client, "bcond", [e])
        row = r.data["entries"][0]
        self.assertEqual(row["status_code"], 422)
        self.assertIn("crack_count", row["errors"])

    def test_batch_limit_50(self):
        entries = [self.entry() for _ in range(51)]
        r = submit_entry(self.client, "bbig", entries)
        self.assertEqual(r.status_code, 400)

    def test_batch_limit_exactly_50_accepted(self):
        entries = [self.entry() for _ in range(50)]
        r = submit_entry(self.client, "b50", entries)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data["entries"]), 50)

    def test_unavailable_template_version(self):
        e = self.entry()
        e["template_version"] = 99
        r = submit_entry(self.client, "btpl", [e])
        self.assertEqual(r.data["entries"][0]["status_code"], 422)
        self.assertIn(
            "template_version_unavailable",
            r.data["entries"][0]["errors"]["template_version"],
        )

    def test_retained_results_fetchable_after_the_fact(self):
        e = self.entry()
        submit_entry(self.client, "persist-batch", [e])
        r = self.client.get("/api/sync/batches/persist-batch/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["entries"][0]["record_version"], 1)
        self.assertTrue(r.data["entries"][0]["content_hash"])

    def test_other_users_cannot_read_batch_results(self):
        e = self.entry()
        submit_entry(self.client, "private-batch", [e])
        other = make_user("w2", Role.WORKER)
        assign(other, self.project, self.crew)
        r = api_for(other).get("/api/sync/batches/private-batch/")
        self.assertEqual(r.status_code, 404)

    def test_submitting_to_deleted_record_is_rejected(self):
        from forms.models import Crew as _Crew  # noqa: F401

        e = self.entry()
        submit_entry(self.client, "bd1", [e])
        sup = make_user("sup", Role.SUPERVISOR)
        assign(sup, self.project, self.crew)
        dr = api_for(sup).delete(f"/api/sync/records/{e['uuid']}/delete/")
        self.assertEqual(dr.status_code, 200)
        r = submit_entry(self.client, "bd2", [e])
        self.assertEqual(r.data["entries"][0]["status_code"], 409)
        self.assertIn("record_deleted", r.data["entries"][0]["errors"]["uuid"])

    def test_cross_template_uuid_reuse_rejected(self):
        e = self.entry()
        submit_entry(self.client, "bx1", [e])
        other_template = FormTemplate.objects.create(
            project=self.project, code="other", name="Other"
        )
        other_template.publish_version(V1_FIELDS, published_by=None)
        r = submit_entry(
            self.client,
            "bx2",
            [
                {
                    **self.entry(record_uuid=e["uuid"]),
                    "template_id": other_template.id,
                    "template_version": 1,
                }
            ],
        )
        self.assertEqual(r.data["entries"][0]["status_code"], 422)

    def test_duplicate_uuid_within_batch_does_not_duplicate(self):
        e = self.entry()
        r = submit_entry(self.client, "bdup", [e, dict(e)])
        codes = [row["status_code"] for row in r.data["entries"]]
        self.assertEqual(codes[0], 201)
        # Same content -> original result surfaced for the duplicate row.
        self.assertEqual(codes[1], 201)
        self.assertEqual(FormRecord.objects.count(), 1)
