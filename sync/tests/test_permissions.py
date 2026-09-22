import uuid

from django.test import TestCase

from accounts.models import Role
from forms.models import FormTemplate
from sync.tests.factories import (
    V1_FIELDS,
    api_for,
    assign,
    make_project_crew,
    make_user,
    submit_entry,
)


def data():
    return {
        "structure_id": "S-1",
        "surface": "steel",
        "crack_count": None,
        "inspection_date": "2026-09-20",
        "notes": "n",
    }


class AuthorizationTests(TestCase):
    def setUp(self):
        self.project, self.crew = make_project_crew()
        self.template = FormTemplate.objects.create(
            project=self.project, code="deck", name="Deck"
        )
        self.v1 = self.template.publish_version(V1_FIELDS, published_by=None)

    def _entry(self):
        return [
            {
                "uuid": str(uuid.uuid4()),
                "template_id": self.template.id,
                "template_version": 1,
                "record_version": 0,
                "crew_id": self.crew.id,
                "collected_at": "2026-09-20T08:00:00Z",
                "data": data(),
            }
        ]

    def test_unauthenticated_rejected(self):
        from rest_framework.test import APIClient

        self.assertEqual(APIClient().post("/api/sync/submit/", {}, format="json").status_code, 401)
        self.assertEqual(APIClient().get("/api/sync/pull/").status_code, 401)

    def test_worker_without_project_assignment_cannot_submit(self):
        outsider = make_user("outsider", Role.WORKER)
        r = submit_entry(api_for(outsider), "x1", self._entry())
        self.assertEqual(r.data["entries"][0]["status_code"], 403)

    def test_worker_assigned_to_project_but_not_crew_cannot_submit(self):
        other_project, other_crew = make_project_crew("P2", "C2")
        half = make_user("half", Role.WORKER)
        assign(half, self.project)
        assign(half, other_project, other_crew)
        r = submit_entry(api_for(half), "x2", self._entry())
        self.assertEqual(r.data["entries"][0]["status_code"], 403)

    def test_pull_does_not_leak_other_project_rows(self):
        insider = make_user("in", Role.WORKER)
        assign(insider, self.project, self.crew)
        submit_entry(api_for(insider), "inside-b", self._entry())

        other_project, other_crew = make_project_crew("P3", "C3")
        other_template = FormTemplate.objects.create(
            project=other_project, code="deck2", name="Deck2"
        )
        other_template.publish_version(V1_FIELDS, published_by=None)
        outsider = make_user("out", Role.WORKER)
        assign(outsider, other_project, other_crew)
        submit_entry(
            api_for(outsider),
            "outside-b",
            [
                {
                    "uuid": str(uuid.uuid4()),
                    "template_id": other_template.id,
                    "template_version": 1,
                    "record_version": 0,
                    "crew_id": other_crew.id,
                    "collected_at": "2026-09-20T08:00:00Z",
                    "data": data(),
                }
            ],
        )

        r = api_for(insider).get("/api/sync/pull/?limit=100")
        self.assertEqual(len(r.data["changes"]), 1)
        r = api_for(outsider).get("/api/sync/pull/?limit=100")
        self.assertEqual(len(r.data["changes"]), 1)

    def test_worker_cannot_see_admin_endpoints(self):
        worker = make_user("w", Role.WORKER)
        c = api_for(worker)
        self.assertEqual(c.post("/api/projects/", {"name": "X"}, format="json").status_code, 403)
        self.assertEqual(
            c.post(
                f"/api/templates/{self.template.id}/versions/publish/",
                {"fields": V1_FIELDS},
                format="json",
            ).status_code,
            403,
        )

    def test_record_detail_missing_for_non_members(self):
        insider = make_user("in2", Role.WORKER)
        assign(insider, self.project, self.crew)
        e = self._entry()
        submit_entry(api_for(insider), "secret-b", e)

        stranger = make_user("stranger", Role.WORKER)
        r = api_for(stranger).get(f"/api/sync/records/{e[0]['uuid']}/")
        self.assertEqual(r.status_code, 404)
