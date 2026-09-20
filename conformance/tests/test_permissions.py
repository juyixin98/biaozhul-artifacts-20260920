"""Project-scoped authorization and reporting endpoints."""

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from conformance.models import Project
from conformance.services import import_events

from .base import ev, make_project, make_template


class PermissionTests(TestCase):
    def setUp(self):
        self.user, self.project = make_project()
        self.template, self.version = make_template(self.project)
        self.outsider = User.objects.create_user("outsider", password="pw12345")
        self.other_project = Project.objects.create(name="other")

        import_events(
            self.project,
            [
                ev("e1", "C1", "a", "2026-09-01T00:00:00Z", 1, self.version),
                ev("e2", "C1", "b", "2026-09-01T05:00:00Z", 2, self.version),
            ],
        )

    def as_user(self, user):
        client = APIClient()
        if user is not None:
            client.force_authenticate(user)
        return client

    def test_member_can_access_project_endpoints(self):
        client = self.as_user(self.user)
        for url in [
            f"/api/projects/{self.project.id}/templates/",
            f"/api/projects/{self.project.id}/cases/",
            f"/api/projects/{self.project.id}/cases/C1/trace/",
            f"/api/projects/{self.project.id}/cases/C1/deviations/",
            f"/api/projects/{self.project.id}/cases/C1/analysis-history/",
            f"/api/projects/{self.project.id}/reports/deviations/",
        ]:
            resp = client.get(url)
            self.assertEqual(resp.status_code, 200, url)

    def test_outsider_forbidden_everywhere(self):
        client = self.as_user(self.outsider)
        for url in [
            f"/api/projects/{self.project.id}/templates/",
            f"/api/projects/{self.project.id}/cases/",
            f"/api/projects/{self.project.id}/cases/C1/trace/",
            f"/api/projects/{self.project.id}/cases/C1/deviations/",
            f"/api/projects/{self.project.id}/reports/deviations/",
        ]:
            resp = client.get(url)
            self.assertEqual(resp.status_code, 403, url)
        resp = client.post(
            f"/api/projects/{self.project.id}/events/import/",
            {"events": []},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)
        resp = client.post(f"/api/projects/{self.project.id}/analysis/rebuild/")
        self.assertEqual(resp.status_code, 403)

    def test_unauthenticated_rejected(self):
        client = self.as_user(None)
        resp = client.get(f"/api/projects/{self.project.id}/cases/")
        self.assertIn(resp.status_code, (401, 403))

    def test_project_list_scoped_to_memberships(self):
        client = self.as_user(self.user)
        resp = client.get("/api/projects/")
        self.assertEqual([p["name"] for p in resp.data], ["proj"])

        client = self.as_user(self.outsider)
        resp = client.get("/api/projects/")
        self.assertEqual(resp.data, [])


class ReportTests(TestCase):
    def setUp(self):
        self.user, self.project = make_project()
        self.template, self.version = make_template(self.project)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        import_events(
            self.project,
            [
                ev("e1", "C1", "a", "2026-09-01T00:00:00Z", 1, self.version),
                ev("e2", "C1", "b", "2026-09-01T05:00:00Z", 2, self.version),
                ev("f1", "C2", "a", "2026-09-01T00:00:00Z", 1, self.version),
                ev("f2", "C2", "b", "2026-09-01T00:10:00Z", 2, self.version),
                ev("f3", "C2", "c", "2026-09-01T00:20:00Z", 3, self.version),
                ev("f4", "C2", "e", "2026-09-01T00:30:00Z", 4, self.version),
            ],
        )

    def test_trace_returns_ordered_events_and_binding(self):
        resp = self.client.get(f"/api/projects/{self.project.id}/cases/C2/trace/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            [e["activity"] for e in resp.data["events"]], ["a", "b", "c", "e"]
        )
        self.assertEqual(resp.data["template_version"]["version"], 1)
        self.assertEqual(resp.data["status"], "conformant")

    def test_deviation_report_is_itemized_not_a_score(self):
        resp = self.client.get(
            f"/api/projects/{self.project.id}/reports/deviations/"
        )
        self.assertEqual(resp.status_code, 200)
        cases = {c["case_key"]: c for c in resp.data["cases"]}
        self.assertEqual(cases["C1"]["status"], "nonconformant")
        self.assertEqual(
            [d["type"] for d in cases["C1"]["deviations"]], ["timeout"]
        )
        # concrete evidence, not an aggregate score
        deviation = cases["C1"]["deviations"][0]
        self.assertIn("evidence", deviation)
        self.assertIn("constraint", deviation)
        self.assertNotIn("score", resp.data)
        self.assertEqual(cases["C2"]["status"], "conformant")

    def test_report_filter_by_type(self):
        resp = self.client.get(
            f"/api/projects/{self.project.id}/reports/deviations/?type=timeout"
        )
        cases = {c["case_key"]: c for c in resp.data["cases"]}
        self.assertEqual(len(cases["C1"]["deviations"]), 1)
        self.assertEqual(cases["C2"]["deviations"], [])

    def test_history_endpoint_lists_revisions(self):
        resp = self.client.get(
            f"/api/projects/{self.project.id}/cases/C1/analysis-history/"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)
        self.assertTrue(resp.data[0]["is_current"])
