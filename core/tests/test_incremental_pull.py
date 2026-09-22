"""增量拉取：稳定游标、删除标记、翻页期间新写入不漏、服务重启续传。"""
import uuid

from django.test import override_settings
from rest_framework.authtoken.models import Token

from core.models import ChangeLog, FormRecord, SyncState
from core.tests.base import FieldSnapAPITestCase


@override_settings(SYNC_HWM_ID_GRACE=0)
class IncrementalPullTests(FieldSnapAPITestCase):
    def _seed_records(self, n, prefix="batch"):
        items = [
            self.item(uuid.uuid4(), self.good_data(site_name=f"井{i}"))
            for i in range(n)
        ]
        r = self.push(self.w1, f"{prefix}-{uuid.uuid4()}", items)
        self.assertEqual(r.status_code, 200, r.content)
        return [it["uuid"] for it in items]

    def test_pagination_returns_all_changes_across_pages(self):
        ids = self._seed_records(5)
        changes, _ = self.pull_all(self.w1, page_size=2)
        # 每个新建记录产生一个 record_upserted 事件
        upserted = [c for c in changes if c["kind"] == "record_upserted"]
        self.assertEqual(len(upserted), 5)
        pulled_uuids = {c["record_uuid"] for c in upserted}
        self.assertEqual(pulled_uuids, set(ids))
        # 游标单调
        cids = [c["id"] for c in changes]
        self.assertEqual(cids, sorted(cids))
        self.assertEqual(len(cids), len(set(cids)))

    def test_cursor_is_stable_resuming_returns_only_new(self):
        ids1 = self._seed_records(3, "first")
        changes, hwm = self.pull_all(self.w1, page_size=5)
        self.assertEqual(len(changes), 3)
        last_cursor = changes[-1]["id"]

        # 新一轮同步：新写入 2 条
        ids2 = self._seed_records(2, "second")
        self.auth(self.w1)
        r = self.client.get(
            f"/api/projects/{self.project.id}/sync/pull/?limit=10"
        )
        self.assertEqual(r.status_code, 200)
        new_changes = r.json()["changes"]
        self.assertEqual(len(new_changes), 2)
        self.assertTrue(all(c["id"] > last_cursor for c in new_changes))
        self.assertEqual(
            {c["record_uuid"] for c in new_changes}, set(ids2)
        )
        # 旧数据没有重复推送
        self.assertTrue(
            {c["record_uuid"] for c in new_changes}.isdisjoint(set(ids1))
        )

    def test_new_writes_during_pagination_are_not_lost(self):
        """翻页期间发生新写入：固定 hwm 快照保证不漏，新写入下一轮读到。"""
        ids = self._seed_records(4, "paged")
        self.auth(self.w1)
        url = f"/api/projects/{self.project.id}/sync/pull/"
        r = self.client.get(url + "?limit=2")
        page1 = r.json()
        hwm = page1["hwm"]
        self.assertEqual(len(page1["changes"]), 2)
        self.assertTrue(page1["has_more"])

        # 翻页途中另一名设备写入了新数据
        late_id = self._seed_records(1, "late")

        r = self.client.get(
            url + f"?cursor={page1['next_cursor']}&hwm={hwm}&limit=2"
        )
        page2 = r.json()
        self.assertEqual(len(page2["changes"]), 2)
        # 快照内不包含迟到写入
        self.assertNotIn(late_id[0], [c["record_uuid"] for c in page2["changes"]])
        self.assertFalse(page2["has_more"])

        # 新一轮同步必须看到迟到写入（不漏数据）
        r = self.client.get(url + "?limit=10")
        uuids = {c["record_uuid"] for c in r.json()["changes"]}
        self.assertIn(late_id[0], uuids)
        # 已同步的 4 条不重复
        self.assertEqual(
            len(uuids & set(ids)), 0
        )

    def test_tombstone_delete_event_in_pull(self):
        rec = uuid.UUID(self._seed_records(1)[0])
        self.auth(self.sup)
        r = self.client.post(f"/api/records/{rec}/delete/")
        self.assertEqual(r.status_code, 200)

        changes, _ = self.pull_all(self.w1, page_size=10)
        kinds = {c["kind"] for c in changes}
        self.assertIn("record_deleted", kinds)
        tomb = next(c for c in changes if c["kind"] == "record_deleted")
        self.assertEqual(tomb["record_uuid"], str(rec))
        self.assertTrue(tomb["payload"]["deleted"])

    def test_conflict_and_resolution_events_in_pull(self):
        rec = uuid.uuid4()
        self.push(self.w1, "c1", [self.item(rec, self.good_data(site_name="A"), 1)])
        self.push(self.w2, "c2", [self.item(rec, self.good_data(site_name="B"), 2)])

        from core.models import Conflict
        from core.models import RecordVersion
        conflict = Conflict.objects.get(record__uuid=rec)
        win = RecordVersion.objects.get(content__site_name="B")
        self.auth(self.sup)
        self.client.post(
            f"/api/conflicts/{conflict.id}/resolve/",
            {"winning_version_id": win.id, "note": "采用B"},
            format="json",
        )

        changes, _ = self.pull_all(self.w1, page_size=10)
        kinds = [c["kind"] for c in changes]
        self.assertIn("conflict_detected", kinds)
        self.assertIn("conflict_resolved", kinds)
        # 解决事件携带最终采用的内容
        resolved = next(c for c in changes if c["kind"] == "conflict_resolved")
        self.assertEqual(resolved["payload"]["data"]["site_name"], "B")
        self.assertTrue(resolved["payload"]["resolved"])

    def test_sync_state_resumes_after_server_restart(self):
        self._seed_records(3)
        self.auth(self.w1)
        url = f"/api/projects/{self.project.id}/sync/pull/?limit=2"
        r = self.client.get(url)
        body = r.json()
        self.assertTrue(body["has_more"])

        # 模拟服务重启：新的 API client、不传 cursor，
        # 服务端从 SyncState 恢复游标和 hwm。
        self.client.credentials()
        self.auth(self.w1)
        r = self.client.get(
            f"/api/projects/{self.project.id}/sync/pull/?limit=2"
        )
        # 恢复了未消费完的 hwm 快照，从已保存游标继续
        page = r.json()
        self.assertEqual(len(page["changes"]), 1)
        self.assertFalse(page["has_more"])
        state = SyncState.objects.get(user=self.w1, project=self.project)
        self.assertEqual(state.cursor, page["next_cursor"])

    def test_payload_reads_with_historical_template_version(self):
        rec = uuid.uuid4()
        self.push(
            self.w1,
            "hist",
            [self.item(rec, self.good_data(temp=25), template_version=1)],
        )
        changes, _ = self.pull_all(self.w1, page_size=10)
        event = changes[0]
        self.assertEqual(event["payload"]["template_version"], 1)
        self.assertEqual(event["payload"]["data"]["temp"], 25)

    def test_pull_filters_by_project(self):
        self._seed_records(1)
        # w1 不属于 P2，按越权规则直接 403
        self.auth(self.w1)
        r = self.client.get(f"/api/projects/{self.other_project.id}/sync/pull/")
        self.assertEqual(r.status_code, 403)

    def test_conflict_detected_does_not_change_cursor_contract(self):
        rec = uuid.uuid4()
        self.push(self.w1, "x1", [self.item(rec, self.good_data(), 1)])
        self.push(self.w2, "x2", [self.item(rec, self.good_data(site_name="Z"), 2)])
        changes, _ = self.pull_all(self.w1, page_size=10)
        # upsert + conflict 两个事件，游标连续
        self.assertEqual([c["id"] for c in changes],
                         sorted(c["id"] for c in changes))
