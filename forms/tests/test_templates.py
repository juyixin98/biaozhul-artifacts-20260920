from django.test import TestCase

from accounts.models import Role
from forms.models import (
    FormTemplate,
    Project,
    validate_field_schema,
)
from forms.validation import validate_record_data
from sync.tests.factories import V1_FIELDS, api_for, make_user

from django.core.exceptions import ValidationError as DjangoValidationError


class TemplatePublishTests(TestCase):
    def setUp(self):
        self.admin = make_user("admin", Role.ADMIN)
        self.project = Project.objects.create(name="P")
        self.template = FormTemplate.objects.create(
            project=self.project, code="deck", name="Deck"
        )

    def test_publish_allocates_sequential_versions_and_updates_current(self):
        v1 = self.template.publish_version(V1_FIELDS, published_by=self.admin)
        v2 = self.template.publish_version(
            V1_FIELDS + [{"key": "gps", "label": "GPS", "type": "text"}],
            published_by=self.admin,
        )
        self.assertEqual((v1.version, v2.version), (1, 2))
        self.template.refresh_from_db()
        self.assertEqual(self.template.current_version_id, v2.id)

    def test_published_version_is_immutable(self):
        v1 = self.template.publish_version(V1_FIELDS, published_by=self.admin)
        v1.fields = [{"key": "x", "label": "X", "type": "text"}]
        with self.assertRaises(DjangoValidationError):
            v1.save()
        with self.assertRaises(DjangoValidationError):
            v1.delete()

    def test_rejects_over_150_fields(self):
        fields = [
            {"key": f"f{i}", "label": f"F{i}", "type": "text"} for i in range(151)
        ]
        with self.assertRaises(DjangoValidationError):
            validate_field_schema(fields)

    def test_accepts_exactly_150_fields(self):
        fields = [
            {"key": f"f{i}", "label": f"F{i}", "type": "text"} for i in range(150)
        ]
        self.assertEqual(validate_field_schema(fields), fields)

    def test_all_field_types_plus_conditional_required_rules(self):
        fields = [
            {"key": "a", "label": "A", "type": "text", "required": True},
            {"key": "b", "label": "B", "type": "number"},
            {"key": "c", "label": "C", "type": "enum", "options": ["x", "y"]},
            {"key": "d", "label": "D", "type": "date"},
            {
                "key": "e",
                "label": "E",
                "type": "text",
                "required_if": {"field": "c", "op": "in", "value": ["x"]},
            },
        ]
        validate_field_schema(fields)

    def test_rejects_bad_type_duplicate_key_bad_enum_unknown_ref(self):
        with self.assertRaises(DjangoValidationError):
            validate_field_schema([{"key": "a", "label": "A", "type": "geom"}])
        with self.assertRaises(DjangoValidationError):
            validate_field_schema(
                [
                    {"key": "a", "label": "A", "type": "text"},
                    {"key": "a", "label": "A2", "type": "text"},
                ]
            )
        with self.assertRaises(DjangoValidationError):
            validate_field_schema(
                [{"key": "a", "label": "A", "type": "enum", "options": []}]
            )
        with self.assertRaises(DjangoValidationError):
            validate_field_schema(
                [
                    {"key": "a", "label": "A", "type": "text"},
                    {
                        "key": "b",
                        "label": "B",
                        "type": "text",
                        "required_if": {"field": "missing", "op": "==", "value": "1"},
                    },
                ]
            )


class RecordDataValidationTests(TestCase):
    def setUp(self):
        self.template = FormTemplate.objects.create(
            project=Project.objects.create(name="P"), code="deck", name="Deck"
        )
        self.v1 = self.template.publish_version(V1_FIELDS, published_by=None)

    def test_valid_payload_is_normalized(self):
        outcome, normalized = validate_record_data(
            self.v1,
            {
                "structure_id": "S-1",
                "surface": "concrete",
                "crack_count": "3",
                "inspection_date": "2026-09-01",
                "notes": None,
            },
        )
        self.assertTrue(outcome.ok, outcome.errors)
        self.assertEqual(normalized["crack_count"], 3.0)
        self.assertIsNone(normalized["notes"])

    def test_required_and_type_errors(self):
        outcome, _ = validate_record_data(
            self.v1,
            {"structure_id": "S", "surface": "brick", "inspection_date": "not-a-date"},
        )
        self.assertFalse(outcome.ok)
        self.assertIn("surface", outcome.errors)
        self.assertIn("inspection_date", outcome.errors)

    def test_conditional_required_triggers_only_on_match(self):
        base = {"structure_id": "S", "surface": "steel", "inspection_date": "2026-09-01"}
        outcome, _ = validate_record_data(self.v1, base)
        self.assertTrue(outcome.ok)
        outcome, _ = validate_record_data(self.v1, {**base, "surface": "concrete"})
        self.assertFalse(outcome.ok)
        self.assertIn("crack_count", outcome.errors)

    def test_unknown_field_rejected(self):
        outcome, _ = validate_record_data(
            self.v1,
            {
                "structure_id": "S",
                "surface": "steel",
                "inspection_date": "2026-09-01",
                "future_field": "x",
            },
        )
        self.assertFalse(outcome.ok)
        self.assertIn("future_field", outcome.errors)

    def test_validation_uses_pinned_version_not_current(self):
        # v2 tightens rules (crack_count always required); v1 data must
        # still validate against v1.
        v2_fields = [
            {"key": "structure_id", "label": "Structure ID", "type": "text", "required": True},
            {"key": "crack_count", "label": "Cracks", "type": "number", "required": True},
        ]
        self.template.publish_version(v2_fields, published_by=None)
        outcome, _ = validate_record_data(
            self.v1,
            {"structure_id": "S", "surface": "steel", "inspection_date": "2026-09-01"},
        )
        self.assertTrue(outcome.ok)


class TemplateAPITests(TestCase):
    def setUp(self):
        self.admin = make_user("admin", Role.ADMIN)
        self.admin_client = api_for(self.admin)
        self.project = Project.objects.create(name="P")

    def test_worker_cannot_publish(self):
        from accounts.models import ProjectAssignment
        from sync.tests.factories import make_user

        worker = make_user("w", Role.WORKER)
        ProjectAssignment.objects.create(user=worker, project=self.project)
        t = FormTemplate.objects.create(project=self.project, code="t", name="T")
        resp = api_for(worker).post(
            f"/api/templates/{t.id}/versions/publish/",
            {"fields": V1_FIELDS},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_publish_and_fetch_snapshot(self):
        t = FormTemplate.objects.create(project=self.project, code="t", name="T")
        resp = self.admin_client.post(
            f"/api/templates/{t.id}/versions/publish/",
            {"fields": V1_FIELDS},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data["version"], 1)

        detail = self.admin_client.get(f"/api/templates/{t.id}/versions/1/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(len(detail.data["fields"]), 5)
