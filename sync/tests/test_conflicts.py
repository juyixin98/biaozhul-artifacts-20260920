from django.test import TestCase

from accounts.models import Role
from forms.models import FormTemplate
from sync.models import FormRecord, RecordStatus, RevisionKind
from sync.tests.factories import (
    V1_FIELDS,
    api_for,
    assign,
    make_project_crew,
    make_user,
    submit_entry,
)
import uuid


def data(**ov):
    base = {
        "structure_id": "S-1",
        "surface": "steel",
        "crack_count": None,
        "inspection_date": "2026-09-20",
        "notes": "a",
    }
    base.update(ov)
    return base


class ConflictResolutionTests(TestCase):
    def setUp(self):
        self.project, self.crew = make_project_crew()
        self.worker = make_user("w", Role.WORKER)
        assign(self.worker, self.project, self.crew)
        self.wclient = api_for(self.worker)

        self.sup = make_user("sup", Role.SUPERVISOR)
        assign(self.sup, self.project, self.crew)
        self.sclient = api_for(self.sup)

        self.template = FormTemplate.objects.create(
            project=self.project, code="deck", name="Deck"
        )
        self.v1 = self.template.publish_version(V1_FIELDS, published_by=None)
        self.u = uuid.uuid4()
        submit_entry(
            self.wclient,
            "c1",
            [
                {
                    "uuid": str(self.u),
                    "template_id": self.template.id,
                    "template_version": 1,
                    "record_version": 0,
                    "crew_id": self.crew.id,
                    "collected_at": "2026-09-20T08:00:00Z",
                    "data": data(notes="original"),
                }
            ],
        )
        submit_entry(
            self.wclient,
            "c2",
            [
                {
                    "uuid": str(self.u),
                    "template_id": self.template.id,
                    "template_version": 1,
                    "record_version": 0,
                    "crew_id": self.crew.id,
                    "collected_at": "2026-09-20T09:00:00Z",
                    "data": data(notes="device-b"),
                }
            ],
        )

    def test_conflict_listed_for_supervisor_only(self):
        r = self.sclient.get("/api/sync/conflicts/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data["conflicts"]), 1)
        item = r.data["conflicts"][0]
        self.assertEqual(item["conflict_of_seq"], 1)
        self.assertEqual({rev["kind"] for rev in item["revisions"]}, {"created", "divergent"})

        rw = self.wclient.get("/api/sync/conflicts/")
        self.assertEqual(rw.status_code, 403)

    def test_supervisor_resolution_creates_authoritative_rev_with_note(self):
        r = self.sclient.post(
            f"/api/sync/conflicts/{self.u}/resolve/",
            {
                "merged_data": data(notes="merged by supervisor"),
                "template_version": 1,
                "note": "kept structure id, used device B note and added sign-off",
            },
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["status"], RecordStatus.OK)
        self.assertEqual(r.data["resolved_revision_seq"], 3)

        record = FormRecord.objects.get(record_uuid=self.u)
        self.assertEqual(record.status, RecordStatus.OK)
        self.assertIsNone(record.conflict_of_id)
        self.assertEqual(record.current_revision.data["notes"], "merged by supervisor")
        resolved = record.revisions.get(seq=3)
        self.assertEqual(resolved.kind, RevisionKind.RESOLVED)
        self.assertEqual(resolved.resolved_by_id, self.sup.id)
        self.assertIn("device B", resolved.resolution_note)
        # Nothing was overwritten: both originals survive.
        notes = set(record.revisions.values_list("data__notes", flat=True))
        self.assertEqual(notes, {"original", "device-b", "merged by supervisor"})

    def test_resolution_choosing_existing_side_does_not_create_dup(self):
        r = self.sclient.post(
            f"/api/sync/conflicts/{self.u}/resolve/",
            {
                "merged_data": data(notes="device-b"),
                "template_version": 1,
                "note": "device B wins per photo evidence",
            },
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        record = FormRecord.objects.get(record_uuid=self.u)
        self.assertEqual(record.revisions.count(), 2)  # no duplicate revision
        self.assertEqual(record.current_revision.data["notes"], "device-b")
        chosen = record.revisions.get(seq=2)
        self.assertEqual(chosen.resolved_by_id, self.sup.id)
        self.assertTrue(chosen.resolution_note)

    def test_resolution_requires_note_and_validates_merged_data(self):
        r = self.sclient.post(
            f"/api/sync/conflicts/{self.u}/resolve/",
            {"merged_data": data(notes="x"), "template_version": 1, "note": ""},
            format="json",
        )
        self.assertEqual(r.status_code, 400)

        r = self.sclient.post(
            f"/api/sync/conflicts/{self.u}/resolve/",
            {
                "merged_data": data(surface="brick"),
                "template_version": 1,
                "note": "bad merge",
            },
            format="json",
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("surface", r.data["errors"])

    def test_normal_edit_blocked_while_conflict_open(self):
        r = submit_entry(
            self.wclient,
            "c3",
            [
                {
                    "uuid": str(self.u),
                    "template_id": self.template.id,
                    "template_version": 1,
                    "record_version": 1,
                    "crew_id": self.crew.id,
                    "collected_at": "2026-09-20T10:00:00Z",
                    "data": data(notes="trying anyway"),
                }
            ],
        )
        self.assertEqual(r.data["entries"][0]["status_code"], 409)
        self.assertIn("record_has_conflict", r.data["entries"][0]["errors"]["__all__"])

    def test_edits_flow_after_resolution(self):
        self.sclient.post(
            f"/api/sync/conflicts/{self.u}/resolve/",
            {"merged_data": data(notes="merged"), "template_version": 1, "note": "done"},
            format="json",
        )
        r = submit_entry(
            self.wclient,
            "c4",
            [
                {
                    "uuid": str(self.u),
                    "template_id": self.template.id,
                    "template_version": 1,
                    "record_version": 3,
                    "crew_id": self.crew.id,
                    "collected_at": "2026-09-20T11:00:00Z",
                    "data": data(notes="post resolution"),
                }
            ],
        )
        self.assertEqual(r.data["entries"][0]["status_code"], 200)

    def test_supervisor_of_other_crew_cannot_resolve(self):
        other_project, other_crew = make_project_crew("Other", "Crew B")
        other_sup = make_user("osup", Role.SUPERVISOR)
        assign(other_sup, other_project, other_crew)
        r = api_for(other_sup).post(
            f"/api/sync/conflicts/{self.u}/resolve/",
            {"merged_data": data(), "template_version": 1, "note": "intruder"},
            format="json",
        )
        self.assertEqual(r.status_code, 403)
