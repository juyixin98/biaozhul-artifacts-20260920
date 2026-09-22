"""并发修改：同 UUID 不同内容保留双方版本，主管显式解决。"""
import threading
import uuid

from django.db import connection
from django.test import TransactionTestCase, override_settings
from rest_framework import status
from rest_framework.authtoken.models import Token

from core.models import (
    Conflict,
    FormRecord,
    FormTemplate,
    Profile,
    Project,
    RecordVersion,
    Team,
)
from core.services.sync import process_batch
from core.tests.base import FieldSnapAPITestCase

from django.contrib.auth import get_user_model

User = get_user_model()


@override_settings(SYNC_HWM_ID_GRACE=0)
class ConcurrentEditTests(FieldSnapAPITestCase):
    def _two_divergent_versions(self):
        rec = uuid.uuid4()
        r1 = self.push(
            self.w1,
            "dev1-batch",
            [self.item(rec, self.good_data(site_name="设备A内容"), 1)],
            device_id="device-A",
        )
        self.assertEqual(r1.json()["results"][0]["status"], "created")

        r2 = self.push(
            self.w2,
            "dev2-batch",
            [self.item(rec, self.good_data(site_name="设备B内容"), 2)],
            device_id="device-B",
        )
        result2 = r2.json()["results"][0]
        self.assertEqual(result2["status"], "conflict")
        return rec, result2

    def test_conflict_keeps_both_versions_and_never_overwrites(self):
        rec, result2 = self._two_divergent_versions()
        record = FormRecord.objects.get(uuid=rec)
        self.assertEqual(record.status, FormRecord.Status.IN_CONFLICT)
        versions = list(
            RecordVersion.objects.filter(record=record).order_by("id")
        )
        self.assertEqual(len(versions), 2)
        contents = {v.content["site_name"] for v in versions}
        self.assertEqual(contents, {"设备A内容", "设备B内容"})
        # 首版本未被覆盖
        self.assertEqual(versions[0].status, RecordVersion.Status.ACCEPTED)
        self.assertEqual(versions[1].status, RecordVersion.Status.CANDIDATE)

        conflict = Conflict.objects.get(record=record, status="open")
        self.assertEqual(conflict.versions.count(), 2)
        self.assertEqual(result2["conflict_id"], conflict.id)

    def test_third_same_content_is_still_idempotent_during_conflict(self):
        rec, _ = self._two_divergent_versions()
        # 设备 A 重试自己原来的提交：必须仍是幂等，不再加版本
        r = self.push(
            self.w1,
            "dev1-retry",
            [self.item(rec, self.good_data(site_name="设备A内容"), 1)],
            device_id="device-A",
        )
        self.assertEqual(r.json()["results"][0]["status"], "idempotent")
        self.assertEqual(
            RecordVersion.objects.filter(record__uuid=rec).count(), 2
        )

    def test_supervisor_resolves_with_note_and_basis(self):
        rec, _ = self._two_divergent_versions()
        conflict = Conflict.objects.get(record__uuid=rec, status="open")
        winning = RecordVersion.objects.get(content__site_name="设备B内容")

        # 解决依据必填
        self.auth(self.sup)
        r = self.client.post(
            f"/api/conflicts/{conflict.id}/resolve/",
            {"winning_version_id": winning.id, "note": "x"},
            format="json",
        )
        self.assertEqual(r.status_code, 400)

        r = self.client.post(
            f"/api/conflicts/{conflict.id}/resolve/",
            {"winning_version_id": winning.id,
             "note": "现场电话核对，设备B为修正后数据，设备A为误填"},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        conflict.refresh_from_db()
        record = FormRecord.objects.get(uuid=rec)
        self.assertEqual(record.status, FormRecord.Status.RESOLVED)
        self.assertEqual(record.current_version_id, winning.id)
        winning.refresh_from_db()
        loser = RecordVersion.objects.get(content__site_name="设备A内容")
        self.assertEqual(winning.status, RecordVersion.Status.ACCEPTED)
        self.assertEqual(loser.status, RecordVersion.Status.SUPERSEDED)
        # 落选版本内容仍然保留（审计依据）
        self.assertEqual(loser.content["site_name"], "设备A内容")

    def test_worker_cannot_resolve_conflict(self):
        rec, _ = self._two_divergent_versions()
        conflict = Conflict.objects.get(record__uuid=rec)
        self.auth(self.w1)
        r = self.client.post(
            f"/api/conflicts/{conflict.id}/resolve/",
            {"winning_version_id": conflict.versions.first().id,
             "note": "工作人员尝试解决"},
            format="json",
        )
        self.assertEqual(r.status_code, 403)

    def test_supervisor_of_other_team_cannot_resolve(self):
        rec, _ = self._two_divergent_versions()
        conflict = Conflict.objects.get(record__uuid=rec)
        self.auth(self.sup2)  # 二班主管，不属于该项目班组
        r = self.client.post(
            f"/api/conflicts/{conflict.id}/resolve/",
            {"winning_version_id": conflict.versions.first().id,
             "note": "越权尝试"},
            format="json",
        )
        self.assertEqual(r.status_code, 403)

    def test_cannot_choose_version_outside_conflict(self):
        rec, _ = self._two_divergent_versions()
        conflict = Conflict.objects.get(record__uuid=rec)
        self.auth(self.sup)
        # 不存在的版本 -> 400
        r = self.client.post(
            f"/api/conflicts/{conflict.id}/resolve/",
            {"winning_version_id": 999999, "note": "随便填的版本号"},
            format="json",
        )
        self.assertEqual(r.status_code, 400)

        # 属于其他项目记录的版本不可见（越权），也不能用于解决本冲突
        other = self.push(
            self.w1,
            "unrelated",
            [self.item(uuid.uuid4(), self.good_data(site_name="无关记录"))],
        ).json()["results"][0]
        r = self.client.post(
            f"/api/conflicts/{conflict.id}/resolve/",
            {"winning_version_id": other["record_version_id"], "note": "越界选择"},
            format="json",
        )
        self.assertIn(r.status_code, (400, 403))
    def test_double_resolution_rejected(self):
        rec, _ = self._two_divergent_versions()
        conflict = Conflict.objects.get(record__uuid=rec)
        win = RecordVersion.objects.get(content__site_name="设备B内容")
        self.auth(self.sup)
        body = {"winning_version_id": win.id, "note": "首次解决"}
        self.assertEqual(
            self.client.post(
                f"/api/conflicts/{conflict.id}/resolve/", body, format="json"
            ).status_code,
            200,
        )
        r = self.client.post(
            f"/api/conflicts/{conflict.id}/resolve/", body, format="json"
        )
        self.assertEqual(r.status_code, 409)

    def test_delete_while_conflict_open_blocked(self):
        rec, _ = self._two_divergent_versions()
        self.auth(self.sup)
        r = self.client.post(f"/api/records/{rec}/delete/")
        self.assertEqual(r.status_code, 409)

    def test_resubmit_after_delete_rejected(self):
        rec = uuid.uuid4()
        self.push(self.w1, "ok", [self.item(rec, self.good_data())])
        self.auth(self.sup)
        r = self.client.post(f"/api/records/{rec}/delete/")
        self.assertEqual(r.status_code, 200)
        r = self.push(
            self.w1,
            "after-delete",
            [self.item(rec, self.good_data())],
        )
        self.assertEqual(r.json()["results"][0]["status"], "error")
        self.assertEqual(r.json()["results"][0]["code"], "record_deleted")


@override_settings(SYNC_HWM_ID_GRACE=0)
class TrueParallelConflictTests(TransactionTestCase):
    """两台设备真正同时提交同一 UUID（仅 MySQL 下行锁语义可完整验证）。

    SQLite 的 SELECT FOR UPDATE 为空操作，无法模拟行锁竞争，
    因此该用例在 SQLite 下跳过；语义正确性由上面的顺序用例 + MySQL CI 保证。
    """

    def setUp(self):
        self.project = Project.objects.create(code="PX", name="并行项目")
        self.team = Team.objects.create(code="TX", name="并行班组")
        self.team.projects.add(self.project)
        self.tpl = FormTemplate.objects.create(
            project=self.project,
            code="site_check",
            version=1,
            name="t",
            schema={"fields": [
                {"key": "site_name", "type": "text", "required": True},
                {"key": "day", "type": "date", "required": True},
            ]},
            status=FormTemplate.Status.PUBLISHED,
        )
        self.u1 = User.objects.create_user("pa", password="x")
        self.u2 = User.objects.create_user("pb", password="x")
        for u in (self.u1, self.u2):
            u.profile.assigned_projects.add(self.project)

    def test_parallel_divergent_writes(self):
        if connection.vendor == "sqlite":
            self.skipTest("真正的并行行锁竞争需要 MySQL（SELECT FOR UPDATE）")

        rec = uuid.uuid4()
        item1 = {
            "uuid": str(rec),
            "template_code": "site_check",
            "template_version": 1,
            "record_version": 1,
            "collected_at": "2026-09-21T08:00:00Z",
            "data": {"site_name": "A", "day": "2026-09-21"},
        }
        item2 = dict(item1)
        item2["record_version"] = 2
        item2["data"] = {"site_name": "B", "day": "2026-09-21"}

        errors = []
        barrier = threading.Barrier(2)

        def worker(user, batch_id, item):
            barrier.wait()
            try:
                process_batch(user, self.project, batch_id, [item])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=worker, args=(self.u1, "par-1", item1))
        t2 = threading.Thread(target=worker, args=(self.u2, "par-2", item2))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(errors, [])
        record = FormRecord.objects.get(uuid=rec)
        statuses = set(
            RecordVersion.objects.filter(record=record).values_list(
                "content__site_name", flat=True
            )
        )
        self.assertEqual(statuses, {"A", "B"})
        self.assertTrue(
            Conflict.objects.filter(record=record, status="open").exists()
        )
