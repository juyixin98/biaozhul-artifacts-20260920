"""Template definition validation, publishing and version immutability."""

import copy

from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from conformance.models import Case, TemplateVersion
from conformance.template_def import validate_definition

from .base import FLOW_DEFINITION, ev, make_project, make_template


def definition(**overrides):
    d = copy.deepcopy(FLOW_DEFINITION)
    d.update(overrides)
    return d


class DefinitionValidationTests(TestCase):
    def test_valid_definition_is_normalized(self):
        norm = validate_definition(FLOW_DEFINITION)
        self.assertEqual(
            norm["dependencies"][0], {"from": ["a"], "to": "b", "mode": "all"}
        )
        self.assertEqual(norm["exclusive_groups"], [["c", "d"]])

    def test_cycle_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_definition(
                definition(
                    dependencies=[
                        {"from": "a", "to": "b"},
                        {"from": "b", "to": "a"},
                    ]
                )
            )
        self.assertTrue(any("acyclic" in m for m in ctx.exception.messages))

    def test_unknown_activity_rejected(self):
        with self.assertRaises(ValidationError):
            validate_definition(
                definition(dependencies=[{"from": "a", "to": "zzz"}])
            )

    def test_duplicate_activities_rejected(self):
        with self.assertRaises(ValidationError):
            validate_definition(definition(activities=["a", "a", "b"]))

    def test_exclusive_group_unknown_activity_rejected(self):
        with self.assertRaises(ValidationError):
            validate_definition(definition(exclusive_groups=[["c", "nope"]]))

    def test_bad_time_limit_rejected(self):
        with self.assertRaises(ValidationError):
            validate_definition(
                definition(
                    time_limits=[{"from": "a", "to": "b", "max_seconds": -5}]
                )
            )

    def test_self_dependency_rejected(self):
        with self.assertRaises(ValidationError):
            validate_definition(
                definition(dependencies=[{"from": "a", "to": "a"}])
            )


class PublishAndImmutabilityTests(TestCase):
    def setUp(self):
        self.user, self.project = make_project()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_publish_flow_and_immutability(self):
        # create template with draft v1
        resp = self.client.post(
            f"/api/projects/{self.project.id}/templates/",
            {"name": "flow", "definition": FLOW_DEFINITION},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        draft = resp.data["draft"]
        self.assertEqual(draft["status"], "draft")

        # publish
        resp = self.client.post(f"/api/versions/{draft['id']}/publish/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "published")
        self.assertIsNotNone(resp.data["published_at"])

        # published definition cannot be modified
        version = TemplateVersion.objects.get(pk=draft["id"])
        version.definition = definition(activities=["a", "b"])
        with self.assertRaises(ValidationError):
            version.save()

        # and cannot be unpublished
        version = TemplateVersion.objects.get(pk=draft["id"])
        version.status = TemplateVersion.Status.DRAFT
        with self.assertRaises(ValidationError):
            version.save()

    def test_invalid_definition_rejected_on_create(self):
        resp = self.client.post(
            f"/api/projects/{self.project.id}/templates/",
            {"name": "bad", "definition": definition(activities=[])},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("errors", resp.data)

    def test_case_stays_bound_to_its_version(self):
        """A case bound to v1 keeps being analyzed against v1 after v2."""
        from conformance.services import import_events

        template, v1 = make_template(self.project)
        # v2 removes the a->b time limit
        v2_def = definition(time_limits=[])
        v2 = TemplateVersion.objects.create(
            template=template, version=2, definition=v2_def,
            status=TemplateVersion.Status.PUBLISHED,
        )
        import_events(
            self.project,
            [
                ev("e1", "C1", "a", "2026-09-01T00:00:00Z", 1, v1),
                # 2 hours later: violates v1's 1h limit, fine under v2
                ev("e2", "C1", "b", "2026-09-01T02:00:00Z", 2, v1),
            ],
        )
        case = Case.objects.get(project=self.project, case_key="C1")
        self.assertEqual(case.template_version_id, v1.id)
        current = case.analyses.get(is_current=True)
        self.assertEqual(
            [d["type"] for d in current.deviations], ["timeout"]
        )
        self.assertEqual(current.deviations[0]["constraint"]["max_seconds"], 3600)

    def test_new_version_does_not_touch_published(self):
        template, v1 = make_template(self.project)
        resp = self.client.post(
            f"/api/templates/{template.id}/versions/",
            {"definition": FLOW_DEFINITION},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["version"], 2)
        self.assertEqual(resp.data["status"], "draft")
        v1.refresh_from_db()
        self.assertEqual(v1.status, TemplateVersion.Status.PUBLISHED)
