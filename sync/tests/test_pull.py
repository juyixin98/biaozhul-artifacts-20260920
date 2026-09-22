import uuid

from django.test import TestCase

from accounts.models import Role
from forms.models import FormTemplate
from sync.models import ChangeType, RecordChange, RecordStatus
from sync.pull import decode_cursor, encode_cursor, pull_changes
from sync.tests.factories import (
    V1_FIELDS,
    api_for,
    assign,
    make_project_crew,
    make_user,
    submit_entry,
)


def data(notes="n"):
    return {
        "structure_id": "S-1",
        "surface": "steel",
        "crack_count": None,
        "inspection_date": "2026-09-20",
        "notes": notes,
    }


class PullServiceTests(TestCase):
    def setUp(self):
        self.project, self.crew = make_project_crew()
        self.worker = make_user("w", Role.WORKER)
        assign(self.worker, self.project, self.crew)
        self.template = FormTemplate.objects.create(
            project=self.project, code="deck", name="Deck"
        )
        self.v1 = self.template.publish_version(V1_FIELDS, published_by=None)

    def _create(self, u):
        submit_entry(
            api_for(self.worker),
            f"b-{u}",
            [
                {
                    "uuid": str(u),
                    "template_id": self.template.id,
                    "template_version": 1,
                    "record_version": 0,
                    "crew_id": self.crew.id,
                    "collected_at": "2026-09-20T08:00:00Z",
                    "data": data(str(u)),
                }
            ],
        )

    def test_cursor_roundtrip_and_paging_walks_all_changes(self):
        for i in range(5):
            self._create(uuid.uuid4())
        self.assertEqual(RecordChange.objects.count(), 5)

        seen = []
        cursor = None
        pages = 0
        for _ in range(10):
            page = pull_changes(
                allowed_project_ids=[self.project.id],
                allowed_crew_ids=[self.crew.id],
                after_cursor=decode_cursor(cursor),
                limit=2,
            )
            pages += 1
            seen.extend(page["changes"])
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
        self.assertEqual(len(seen), 5)
        self.assertEqual(pages, 3)
        ids = [c["cursor_id"] for c in seen]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), len(set(ids)))

    def test_writes_during_paging_are_not_lost_or_duplicated(self):
        u1, u2 = uuid.uuid4(), uuid.uuid4()
        self._create(u1)
        self._create(u2)

        # Client takes page 1 (limit 2): it has both creates.
        first = pull_changes(
            allowed_project_ids=[self.project.id],
            allowed_crew_ids=[self.crew.id],
            after_cursor=0,
            limit=2,
        )
        self.assertFalse(first["has_more"])

        # New write lands before the client asks for the next page.
        self._create(uuid.uuid4())
        second = pull_changes(
            allowed_project_ids=[self.project.id],
            allowed_crew_ids=[self.crew.id],
            after_cursor=decode_cursor(first["next_cursor"]),
            limit=2,
        )
        self.assertEqual(len(second["changes"]), 1)
        self.assertFalse(second["has_more"])

    def test_tombstones_appear_as_deleted_changes(self):
        u = uuid.uuid4()
        self._create(u)
        sup = make_user("sup", Role.SUPERVISOR)
        assign(sup, self.project, self.crew)
        api_for(sup).delete(f"/api/sync/records/{u}/delete/")

        page = pull_changes(
            allowed_project_ids=[self.project.id],
            allowed_crew_ids=[self.crew.id],
            after_cursor=0,
            limit=10,
        )
        types = [(c["change_type"], c["deleted"]) for c in page["changes"]]
        self.assertIn((ChangeType.DELETED, True), types)

        tomb = [c for c in page["changes"] if c["change_type"] == ChangeType.DELETED][0]
        self.assertEqual(tomb["record_uuid"], str(u))
        self.assertIsNone(tomb["data"])

    def test_conflict_and_resolution_are_in_stream(self):
        u = uuid.uuid4()
        client = api_for(self.worker)
        entry = {
            "uuid": str(u),
            "template_id": self.template.id,
            "template_version": 1,
            "record_version": 0,
            "crew_id": self.crew.id,
            "collected_at": "2026-09-20T08:00:00Z",
            "data": data("a"),
        }
        submit_entry(client, "p1", [entry])
        submit_entry(
            client,
            "p2",
            [{**entry, "data": data("b"), "collected_at": "2026-09-20T09:00:00Z"}],
        )
        sup = make_user("sup2", Role.SUPERVISOR)
        assign(sup, self.project, self.crew)
        api_for(sup).post(
            f"/api/sync/conflicts/{u}/resolve/",
            {"merged_data": data("m"), "template_version": 1, "note": "n"},
            format="json",
        )
        page = pull_changes(
            allowed_project_ids=[self.project.id],
            allowed_crew_ids=[self.crew.id],
            after_cursor=0,
            limit=50,
        )
        stream = [c["change_type"] for c in page["changes"]]
        self.assertEqual(
            stream,
            [ChangeType.CREATED, ChangeType.CONFLICT, ChangeType.RESOLVED],
        )

    def test_invalid_cursor_rejected(self):
        with self.assertRaises(Exception):
            decode_cursor("not-base64!!")


class PullAPITests(TestCase):
    def setUp(self):
        self.project, self.crew = make_project_crew()
        self.worker = make_user("w", Role.WORKER)
        assign(self.worker, self.project, self.crew)
        self.client = api_for(self.worker)
        self.template = FormTemplate.objects.create(
            project=self.project, code="deck", name="Deck"
        )
        self.v1 = self.template.publish_version(V1_FIELDS, published_by=None)

    def test_pull_empty_then_create_then_pull(self):
        r = self.client.get("/api/sync/pull/?limit=10")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["changes"], [])

        u = uuid.uuid4()
        submit_entry(
            self.client,
            "pull-b",
            [
                {
                    "uuid": str(u),
                    "template_id": self.template.id,
                    "template_version": 1,
                    "record_version": 0,
                    "crew_id": self.crew.id,
                    "collected_at": "2026-09-20T08:00:00Z",
                    "data": data(),
                }
            ],
        )
        r = self.client.get("/api/sync/pull/?limit=10")
        self.assertEqual(len(r.data["changes"]), 1)
        self.assertEqual(r.data["changes"][0]["record_uuid"], str(u))
        self.assertEqual(r.data["changes"][0]["template_version"], 1)

    def test_pull_resumes_with_cursor_after_restart(self):
        for i in range(3):
            submit_entry(
                self.client,
                f"restart-{i}",
                [
                    {
                        "uuid": str(uuid.uuid4()),
                        "template_id": self.template.id,
                        "template_version": 1,
                        "record_version": 0,
                        "crew_id": self.crew.id,
                        "collected_at": "2026-09-20T08:00:00Z",
                        "data": data(f"n{i}"),
                    }
                ],
            )
        r1 = self.client.get("/api/sync/pull/?limit=1")
        self.assertTrue(r1.data["has_more"])
        cursor = r1.data["next_cursor"]

        # Simulate restart: a brand new HTTP client with the persisted cursor.
        fresh = api_for(self.worker)
        r2 = fresh.get(f"/api/sync/pull/?cursor={cursor}&limit=1")
        r3 = fresh.get(f"/api/sync/pull/?cursor={r2.data['next_cursor']}&limit=1")
        ids = {
            r1.data["changes"][0]["cursor_id"],
            r2.data["changes"][0]["cursor_id"],
            r3.data["changes"][0]["cursor_id"],
        }
        self.assertEqual(len(ids), 3)
        self.assertFalse(r3.data["has_more"])
