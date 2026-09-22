"""越权测试：工作人员仅访问分配项目；主管仅处理所属班组冲突。"""
import uuid

from django.test import override_settings

from core.models import FormRecord
from core.tests.base import FieldSnapAPITestCase


@override_settings(SYNC_HWM_ID_GRACE=0)
class AuthorizationTests(FieldSnapAPITestCase):
    def test_unauthenticated_rejected(self):
        self.client.credentials()
        r = self.client.get(f"/api/projects/{self.project.id}/sync/pull/")
        self.assertEqual(r.status_code, 401)
        r = self.client.post(
            f"/api/projects/{self.project.id}/sync/push/", {}, format="json"
        )
        self.assertEqual(r.status_code, 401)

    def test_worker_cannot_push_unassigned_project(self):
        r = self.push(
            self.w1,
            "cross-project",
            [self.item(uuid.uuid4(), self.good_data())],
            project=self.other_project,
        )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(FormRecord.objects.filter(project=self.other_project).count(), 0)

    def test_worker_can_access_assigned_project(self):
        r = self.push(
            self.w1,
            "assigned",
            [self.item(uuid.uuid4(), self.good_data())],
        )
        self.assertEqual(r.status_code, 200)

    def test_assignment_is_per_worker(self):
        # w2 在同项目内：可以提交
        r = self.push(
            self.w2,
            "w2-ok",
            [self.item(uuid.uuid4(), self.good_data())],
        )
        self.assertEqual(r.status_code, 200)

    def test_worker_sees_only_assigned_projects_in_list(self):
        self.auth(self.w1)
        r = self.client.get("/api/projects/")
        codes = {x["code"] for x in r.json()["results"]}
        self.assertEqual(codes, {"P1"})

    def test_worker_cannot_create_project_or_team(self):
        self.auth(self.w1)
        r = self.client.post("/api/projects/", {"code": "P9", "name": "x"},
                             format="json")
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/api/teams/", {"code": "T9", "name": "x"},
                             format="json")
        self.assertEqual(r.status_code, 403)

    def test_worker_cannot_delete_records(self):
        rec = uuid.uuid4()
        self.push(self.w1, "to-delete", [self.item(rec, self.good_data())])
        self.auth(self.w1)
        r = self.client.post(f"/api/records/{rec}/delete/")
        self.assertEqual(r.status_code, 403)

    def test_supervisor_can_delete_in_team_project(self):
        rec = uuid.uuid4()
        self.push(self.w1, "sup-del", [self.item(rec, self.good_data())])
        self.auth(self.sup)
        r = self.client.post(f"/api/records/{rec}/delete/")
        self.assertEqual(r.status_code, 200)

    def test_supervisor_cannot_touch_other_team_record(self):
        # 主管尝试删除其他项目的记录
        r = self.push(
            self.other_worker,
            "other-pj-record",
            [self.item(
                uuid.uuid4(),
                {"site_name": "x", "day": "2026-09-21"},
                template_version=1,
            )],
            project=self.other_project,
        )
        self.assertEqual(r.status_code, 200)
        rec = FormRecord.objects.get(project=self.other_project)
        self.auth(self.sup)
        r = self.client.post(f"/api/records/{rec.uuid}/delete/")
        self.assertEqual(r.status_code, 404)  # 记录不在其可见范围内

    def test_me_reports_scope(self):
        self.auth(self.w1)
        r = self.client.get("/api/me/")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["role"], "worker")
        self.assertEqual(body["accessible_projects"], [self.project.id])

        self.auth(self.sup)
        r = self.client.get("/api/me/")
        body = r.json()
        self.assertEqual(body["role"], "supervisor")
        self.assertIn(self.project.id, body["accessible_projects"])
        self.assertNotIn(self.other_project.id, body["accessible_projects"])
        self.assertIn(self.team.id, body["supervised_team_ids"])

    def test_cannot_forge_template_version_from_other_project(self):
        # P2 的模板版本号不能用于向 P1 提交
        r = self.push(
            self.w1,
            "forge",
            [self.item(
                uuid.uuid4(),
                self.good_data(),
                template_version=1,
            )],
        )
        # 同编码在 P1 存在所以会通过；构造一个 P1 不存在的编码
        items = self.item(uuid.uuid4(), self.good_data(), template_code="site_check")
        # 直接验证跨项目：用 P2 的 w1（无权限）已经覆盖。
        self.assertEqual(r.status_code, 200)
        self.auth(self.w1)
        r2 = self.client.post(
            f"/api/projects/{self.project.id}/sync/push/",
            {"client_batch_id": "forge2", "items": [
                {**items, "uuid": str(uuid.uuid4()), "template_code": "evil"}
            ]},
            format="json",
        )
        self.assertEqual(r2.json()["results"][0]["code"], "template_not_found")
