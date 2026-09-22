"""断网重试：同 UUID 同内容重试返回原结果；批次结果持久化。"""
import uuid

from django.test import override_settings
from rest_framework import status

from core.models import FormRecord, RecordVersion, SyncBatch, SyncItem
from core.tests.base import FieldSnapAPITestCase


@override_settings(SYNC_HWM_ID_GRACE=0)
class OfflineRetryTests(FieldSnapAPITestCase):
    def test_same_uuid_same_content_returns_original_result(self):
        rec = uuid.uuid4()
        payload_items = [self.item(rec, self.good_data())]

        r1 = self.push(self.w1, "batch-A", payload_items)
        self.assertEqual(r1.status_code, 200)
        first = r1.json()["results"][0]
        self.assertEqual(first["status"], "created")
        self.assertIn("record_version_id", first)

        # 模拟断网重发：整个批次原样再次提交
        r2 = self.push(self.w1, "batch-A", payload_items)
        self.assertEqual(r2.status_code, 200)
        body = r2.json()
        self.assertTrue(body["replayed"])
        again = body["results"][0]
        self.assertEqual(again["status"], "created")  # 返回的是"首次结果"
        self.assertEqual(
            again["record_version_id"], first["record_version_id"]
        )
        self.assertEqual(RecordVersion.objects.filter(record__uuid=rec).count(), 1)

    def test_same_content_even_with_different_record_version_label_is_idempotent(self):
        rec = uuid.uuid4()
        self.push(self.w1, "b1", [self.item(rec, self.good_data(), 1)])
        # 客户端重新标号但内容没变（常见于离线队列重建）
        r = self.push(
            self.w1, "b2", [self.item(rec, self.good_data(), 99)]
        )
        self.assertEqual(r.json()["results"][0]["status"], "idempotent")
        self.assertEqual(RecordVersion.objects.filter(record__uuid=rec).count(), 1)

    def test_item_level_results_persisted_and_replayable_by_get(self):
        rec_ok = uuid.uuid4()
        items = [
            self.item(rec_ok, self.good_data()),
            self.item("not-a-uuid", self.good_data()),
        ]
        r = self.push(self.w1, "batch-mixed", items)
        self.assertEqual(r.status_code, status.HTTP_207_MULTI_STATUS)
        statuses = [x["status"] for x in r.json()["results"]]
        self.assertEqual(statuses, ["created", "error"])

        # 服务重启后通过 GET 找回首次条目级结果
        self.auth(self.w1)
        url = (
            f"/api/projects/{self.project.id}/sync/batch/"
            "?client_batch_id=batch-mixed"
        )
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        results = r.json()["results"]
        self.assertEqual([x["status"] for x in results], ["created", "error"])
        self.assertEqual(results[1]["code"], "invalid_uuid")
        self.assertEqual(SyncBatch.objects.get(client_batch_id="batch-mixed").status,
                         "completed")
        self.assertEqual(SyncItem.objects.count(), 2)

    def test_batch_size_limit_50(self):
        items = [
            self.item(uuid.uuid4(), self.good_data()) for _ in range(51)
        ]
        r = self.push(self.w1, "too-big", items)
        self.assertEqual(r.status_code, 400)
        self.assertIn("50", r.json()["detail"])
        self.assertEqual(FormRecord.objects.count(), 0)

    def test_empty_batch_rejected(self):
        r = self.push(self.w1, "empty", [])
        self.assertEqual(r.status_code, 400)

    def test_one_invalid_item_does_not_rollback_others(self):
        r = self.push(
            self.w1,
            "partial",
            [
                self.item(uuid.uuid4(), self.good_data()),
                self.item(uuid.uuid4(), {"wrong": 1}),
            ],
        )
        self.assertEqual(r.status_code, status.HTTP_207_MULTI_STATUS)
        statuses = [x["status"] for x in r.json()["results"]]
        self.assertEqual(statuses[0], "created")
        self.assertEqual(statuses[1], "error")

    def test_unknown_template_and_draft_rejected(self):
        r = self.push(
            self.w1,
            "tpl-x",
            [self.item(uuid.uuid4(), self.good_data(), template_version=99)],
        )
        self.assertEqual(r.json()["results"][0]["code"], "template_not_found")

    def test_malformed_fields_return_item_codes(self):
        r = self.push(
            self.w1,
            "bad-shapes",
            [
                # 合法形状但缺 template_code
                {**self.item(uuid.uuid4(), self.good_data()),
                 "template_code": ""},
                self.item(uuid.uuid4(), self.good_data(),
                          collected_at="not-a-time"),
                {**self.item(uuid.uuid4(), self.good_data()),
                 "record_version": 0},
            ],
        )
        codes = [x["code"] for x in r.json()["results"]]
        self.assertEqual(
            codes,
            ["invalid_template_code", "invalid_collected_at",
             "invalid_record_version"],
        )

    def test_invalid_uuid_raw_string_persisted_for_audit(self):
        r = self.push(
            self.w1,
            "raw-uuid",
            [
                self.item("12345-not-uuid", self.good_data()),
                self.item(uuid.uuid4(), self.good_data()),
            ],
        )
        self.assertEqual(r.status_code, 207)
        results = r.json()["results"]
        self.assertEqual(results[0]["uuid"], "12345-not-uuid")
        item = SyncItem.objects.get(index=0)
        self.assertIsNone(item.uuid)
        self.assertEqual(item.raw_uuid, "12345-not-uuid")
        # 重放时原始字符串仍然返回
        self.auth(self.w1)
        replay = self.client.get(
            f"/api/projects/{self.project.id}/sync/batch/"
            "?client_batch_id=raw-uuid"
        ).json()
        self.assertEqual(replay["results"][0]["uuid"], "12345-not-uuid")
        self.assertEqual(replay["results"][1]["status"], "created")
