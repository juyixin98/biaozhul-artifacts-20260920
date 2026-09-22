import uuid

from django.test import TestCase

from accounts.models import Role
from forms.models import FormTemplate, Project
from forms.validation import RecordValidationError, check_version_accepted
from sync.models import FormRecord, RecordChange
from sync.tests.factories import (
    V1_FIELDS,
    api_for,
    assign,
    make_project_crew,
    make_user,
    submit_entry,
)


def v1_data():
    return {
        "structure_id": "S-1",
        "surface": "steel",
        "crack_count": None,
        "inspection_date": "2026-09-20",
        "notes": "n",
    }


class TemplateUpgradeTests(TestCase):
    def setUp(self):
        self.project, self.crew = make_project_crew()
        self.worker = make_user("w", Role.WORKER)
        assign(self.worker, self.project, self.crew)
        self.client = api_for(self.worker)
        self.template = FormTemplate.objects.create(
            project=self.project, code="deck", name="Deck"
        )
        self.v1 = self.template.publish_version(V1_FIELDS, published_by=None)
        self.record_uuid = uuid.uuid4()
        # Old device collects against v1 BEFORE the upgrade.
        submit_entry(
            self.client,
            "old-create",
            [
                {
                    "uuid": str(self.record_uuid),
                    "template_id": self.template.id,
                    "template_version": 1,
                    "record_version": 0,
                    "crew_id": self.crew.id,
                    "collected_at": "2026-09-20T08:00:00Z",
                    "data": v1_data(),
                }
            ],
        )

    def test_added_field_in_v2_keeps_old_device_submissions_valid(self):
        v2_fields = V1_FIELDS + [
            {"key": "gps_lat", "label": "Lat", "type": "number", "required": True},
        ]
        self.template.publish_version(v2_fields, published_by=None)

        # Old device (still pinned to v1) syncs its queued, offline edits:
        # the new required field must not retroactively invalidate them.
        r = submit_entry(
            self.client,
            "old-update",
            [
                {
                    "uuid": str(self.record_uuid),
                    "template_id": self.template.id,
                    "template_version": 1,
                    "record_version": 1,
                    "crew_id": self.crew.id,
                    "collected_at": "2026-09-21T08:00:00Z",
                    "data": {**v1_data(), "notes": "offline edit on old schema"},
                }
            ],
        )
        self.assertEqual(r.data["entries"][0]["status_code"], 200, r.data)

        record = FormRecord.objects.get(record_uuid=self.record_uuid)
        self.assertEqual(record.current_version.version, 1)  # pinned version kept
        # Historical v1 content still reads with the v1 schema attached.
        rev = record.revisions.get(seq=1)
        self.assertEqual(rev.template_version.version, 1)
        self.assertEqual(set(rev.data.keys()), {f["key"] for f in V1_FIELDS})

    def test_field_deletion_does_not_break_old_records(self):
        # v2 removes the "notes" field and adds a new one.
        v2_fields = [f for f in V1_FIELDS if f["key"] != "notes"] + [
            {"key": "weather", "label": "Weather", "type": "enum", "options": ["sun", "rain"]}
        ]
        self.template.publish_version(v2_fields, published_by=None)

        # Old v1 submission stays accepted and readable.
        r = self.client.get(f"/api/sync/records/{self.record_uuid}/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("notes", r.data["revisions"][0]["data"])

        # A v2 client validates under v2 rules; sending deleted "notes" fails.
        r2 = submit_entry(
            self.client,
            "new-schema-bad",
            [
                {
                    "uuid": str(uuid.uuid4()),
                    "template_id": self.template.id,
                    "template_version": 2,
                    "record_version": 0,
                    "crew_id": self.crew.id,
                    "collected_at": "2026-09-22T08:00:00Z",
                    "data": {
                        "structure_id": "S-2",
                        "surface": "wood",
                        "crack_count": None,
                        "inspection_date": "2026-09-22",
                        "notes": "deleted field",
                    },
                }
            ],
        )
        self.assertEqual(r2.data["entries"][0]["status_code"], 422)
        self.assertIn("notes", r2.data["entries"][0]["errors"])

        # And the same v2 client succeeds without it.
        r3 = submit_entry(
            self.client,
            "new-schema-ok",
            [
                {
                    "uuid": str(uuid.uuid4()),
                    "template_id": self.template.id,
                    "template_version": 2,
                    "record_version": 0,
                    "crew_id": self.crew.id,
                    "collected_at": "2026-09-22T09:00:00Z",
                    "data": {
                        "structure_id": "S-2",
                        "surface": "wood",
                        "crack_count": None,
                        "inspection_date": "2026-09-22",
                        "weather": "sun",
                    },
                }
            ],
        )
        self.assertEqual(r3.data["entries"][0]["status_code"], 201)

    def test_tightened_min_supported_version_rejects_stale_pins_with_clear_error(self):
        # Admin declares anything below v2 unsupported (e.g. a deleted legal
        # field makes v1 data ambiguous) while still not deleting v1 rows.
        v2_fields = V1_FIELDS + [{"key": "weather", "label": "W", "type": "text"}]
        v2 = self.template.publish_version(
            v2_fields, published_by=None, min_supported_version=2
        )
        self.assertEqual(v2.min_supported_version, 2)

        with self.assertRaises(RecordValidationError):
            check_version_accepted(self.template, self.v1)

        r = submit_entry(
            self.client,
            "too-old",
            [
                {
                    "uuid": str(self.record_uuid),
                    "template_id": self.template.id,
                    "template_version": 1,
                    "record_version": 1,
                    "crew_id": self.crew.id,
                    "collected_at": "2026-09-23T08:00:00Z",
                    "data": v1_data(),
                }
            ],
        )
        row = r.data["entries"][0]
        self.assertEqual(row["status_code"], 422)
        self.assertIn("minimum supported version", row["errors"]["__all__"])

        # Existing historical data is untouched and still readable.
        self.assertEqual(
            FormRecord.objects.get(record_uuid=self.record_uuid).revisions.count(), 1
        )

    def test_pull_after_upgrade_still_carries_old_version_records(self):
        self.template.publish_version(
            V1_FIELDS + [{"key": "weather", "label": "W", "type": "text"}],
            published_by=None,
        )
        r = self.client.get("/api/sync/pull/?limit=10")
        changes = r.data["changes"]
        self.assertTrue(any(c["template_version"] == 1 for c in changes))
