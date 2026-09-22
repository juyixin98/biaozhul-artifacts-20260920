"""模板升级：发布版本不可变；字段删除/规则收紧的兼容策略；旧提交不丢。"""
import uuid

from django.test import override_settings

from core.models import FormTemplate, RecordVersion
from core.tests.base import FieldSnapAPITestCase


@override_settings(SYNC_HWM_ID_GRACE=0)
class TemplateUpgradeTests(FieldSnapAPITestCase):
    def _new_draft(self, fields, version_notes=""):
        self.auth(self.admin)
        r = self.client.post(
            f"/api/templates/{self.template.id}/new_draft/",
            {"version_notes": version_notes},
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.content)
        draft_id = r.json()["id"]
        r = self.client.patch(
            f"/api/templates/{draft_id}/",
            {"schema": {"fields": fields}},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        return draft_id

    def test_published_template_is_immutable(self):
        self.auth(self.admin)
        r = self.client.patch(
            f"/api/templates/{self.template.id}/",
            {"name": "改个名"},
            format="json",
        )
        self.assertEqual(r.status_code, 409)
        r = self.client.put(
            f"/api/templates/{self.template.id}/",
            {"name": "x", "schema": {"fields": []}},
            format="json",
        )
        self.assertEqual(r.status_code, 409)
        r = self.client.delete(f"/api/templates/{self.template.id}/")
        self.assertEqual(r.status_code, 409)

    def test_field_count_limit_150(self):
        fields = [
            {"key": f"f{i}", "type": "text"} for i in range(151)
        ]
        draft_id = self._new_draft(fields) if False else None
        self.auth(self.admin)
        r = self.client.post(
            "/api/templates/",
            {
                "project": self.other_project.id,
                "code": "huge",
                "name": "超字段表",
                "schema": {"fields": fields},
            },
            format="json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("150", str(r.json()))

    def test_deleted_field_becomes_legacy_and_old_device_submission_survives(self):
        rec = uuid.uuid4()
        # 旧设备先按 v1 提交了含 temp 字段的记录
        r = self.push(
            self.w1,
            "old-submission",
            [self.item(
                rec,
                self.good_data(temp=30),
                template_version=1,
            )],
        )
        self.assertEqual(r.json()["results"][0]["status"], "created")

        # v2 删除 temp、新增选填 remark
        v2_fields = [
            {"key": "site_name", "label": "工地", "type": "text", "required": True},
            {"key": "phase", "label": "阶段", "type": "enum",
             "options": ["foundation", "framing", "finishing"]},
            {"key": "day", "label": "日期", "type": "date", "required": True},
            {"key": "remark", "label": "备注", "type": "text"},
        ]
        draft_id = self._new_draft(v2_fields, "删除温度字段")
        self.auth(self.admin)
        r = self.client.post(f"/api/templates/{draft_id}/publish/")
        self.assertEqual(r.status_code, 200, r.content)
        report = r.json()["compatibility"]
        self.assertIn("temp", report["legacy_fields"])
        self.assertTrue(any("temp" in w for w in report["warnings"]))

        # 旧版本自动弃用，但旧设备仍按 v1 提交：不丢、不报错
        self.template.refresh_from_db()
        self.assertEqual(self.template.status, FormTemplate.Status.DEPRECATED)
        r = self.push(
            self.w1,
            "old-device-after-upgrade",
            [self.item(
                uuid.uuid4(),
                self.good_data(temp=40),
                template_version=1,
            )],
        )
        self.assertEqual(r.json()["results"][0]["status"], "created")

        # 同一条旧记录继续按 v1 重试，内容仍被接受（幂等）
        r = self.push(
            self.w1,
            "old-record-retry",
            [self.item(rec, self.good_data(temp=30), template_version=1)],
        )
        self.assertEqual(r.json()["results"][0]["status"], "idempotent")

        # 新设备按 v2 提交：不允许带已删除的 temp（不在 legacy 校验里——
        # 注意 legacy 是允许旧数据的白名单；v2 客户端本不应发送它。
        # 按兼容策略：legacy 字段对任何提交都原样放行，旧设备零升级成本）
        r = self.push(
            self.w1,
            "new-device",
            [self.item(
                uuid.uuid4(),
                {"site_name": "新井", "day": "2026-09-22", "remark": "ok"},
                template_version=2,
            )],
        )
        self.assertEqual(r.json()["results"][0]["status"], "created")

    def test_type_change_and_enum_removal_block_publication(self):
        # 改变字段类型 -> 阻断
        bad_fields = [
            {"key": "site_name", "type": "number", "required": True},
            {"key": "phase", "type": "enum",
             "options": ["foundation", "framing", "finishing"]},
            {"key": "day", "type": "date", "required": True},
        ]
        draft_id = self._new_draft(bad_fields)
        self.auth(self.admin)
        r = self.client.post(f"/api/templates/{draft_id}/publish/")
        self.assertEqual(r.status_code, 409)
        self.assertTrue(any("site_name" in b for b in r.json()["blocking"]))

        # 枚举选项删除 -> 阻断（先删除被阻断的草稿，再派生新草稿）
        self.auth(self.admin)
        self.assertEqual(
            self.client.delete(f"/api/templates/{draft_id}/").status_code, 204
        )
        bad_enum = [
            {"key": "site_name", "type": "text", "required": True},
            {"key": "phase", "type": "enum",
             "options": ["foundation", "framing"]},  # finishing 被删
            {"key": "day", "type": "date", "required": True},
        ]
        draft_id2 = self._new_draft(bad_enum)
        r = self.client.post(f"/api/templates/{draft_id2}/publish/")
        self.assertEqual(r.status_code, 409)
        self.assertTrue(any("phase" in b for b in r.json()["blocking"]))

    def test_tightened_required_rule_only_applies_to_new_version(self):
        # v2 新增必填字段 manager
        fields = [
            {"key": "site_name", "type": "text", "required": True},
            {"key": "phase", "type": "enum",
             "options": ["foundation", "framing", "finishing"]},
            {"key": "day", "type": "date", "required": True},
            {"key": "manager", "type": "text", "required": True},
        ]
        draft_id = self._new_draft(fields)
        self.auth(self.admin)
        r = self.client.post(f"/api/templates/{draft_id}/publish/")
        self.assertEqual(r.status_code, 200)

        # 旧设备按 v1 提交，没有 manager：依然合法（按当时版本校验）
        r = self.push(
            self.w1,
            "old-version-ok",
            [self.item(uuid.uuid4(), self.good_data(), template_version=1)],
        )
        self.assertEqual(r.json()["results"][0]["status"], "created")

        # 按 v2 提交缺 manager：失败
        r = self.push(
            self.w1,
            "new-version-missing",
            [self.item(uuid.uuid4(), self.good_data(), template_version=2)],
        )
        item = r.json()["results"][0]
        self.assertEqual(item["status"], "error")
        self.assertEqual(item["code"], "validation_failed")
        self.assertIn("manager", item["detail"])

    def test_conditional_required_enforced_by_version(self):
        # v1 下 finishing 必须填 issue_note
        r = self.push(
            self.w1,
            "cond-required",
            [self.item(
                uuid.uuid4(),
                self.good_data(phase="finishing"),
                template_version=1,
            )],
        )
        item = r.json()["results"][0]
        self.assertEqual(item["status"], "error")
        self.assertIn("issue_note", item["detail"])

        r = self.push(
            self.w1,
            "cond-required-ok",
            [self.item(
                uuid.uuid4(),
                self.good_data(phase="finishing", issue_note="墙面裂缝已记录"),
                template_version=1,
            )],
        )
        self.assertEqual(r.json()["results"][0]["status"], "created")

    def test_worker_cannot_publish_or_create_templates(self):
        self.auth(self.w1)
        r = self.client.post(
            "/api/templates/",
            {"project": self.project.id, "code": "x", "name": "x",
             "schema": {"fields": [{"key": "a", "type": "text"}]}},
            format="json",
        )
        self.assertEqual(r.status_code, 403)
        r = self.client.post(f"/api/templates/{self.template.id}/publish/")
        self.assertEqual(r.status_code, 403)

    def test_supervisor_of_team_can_publish_project_template(self):
        draft_id = self._new_draft(
            [
                {"key": "site_name", "type": "text", "required": True},
                {"key": "day", "type": "date", "required": True},
            ]
        )
        self.auth(self.sup)  # 一班主管 -> 属于该项目
        r = self.client.post(f"/api/templates/{draft_id}/publish/")
        self.assertEqual(r.status_code, 200, r.content)

    def test_supervisor_cannot_publish_other_project_template(self):
        # 主管 sup 是一班主管，other_template 属于 P2；
        # 用一个不属于任何班组的主管账号验证
        from django.contrib.auth import get_user_model
        from core.models import Profile
        free_sup = get_user_model().objects.create_user("freesup", password="x")
        free_sup.profile.role = Profile.Role.SUPERVISOR
        free_sup.profile.save()
        from rest_framework.authtoken.models import Token
        Token.objects.create(user=free_sup)
        self.auth(free_sup)
        r = self.client.post(
            f"/api/templates/{self.other_template.id}/new_draft/",
            {}, format="json",
        )
        self.assertEqual(r.status_code, 403)

    def test_number_range_and_date_enum_validated(self):
        r = self.push(
            self.w1,
            "bad-ranges",
            [self.item(
                uuid.uuid4(),
                self.good_data(temp=999, phase="nope", day="2026-9-1"),
            )],
        )
        detail = r.json()["results"][0]["detail"]
        self.assertIn("temp", detail)
        self.assertIn("phase", detail)
        self.assertIn("day", detail)
        self.assertEqual(RecordVersion.objects.count(), 0)

    def test_duplicate_draft_creation_rejected(self):
        self._new_draft(self.template.field_defs)
        self.auth(self.admin)
        r = self.client.post(f"/api/templates/{self.template.id}/new_draft/",
                             {}, format="json")
        self.assertEqual(r.status_code, 409)
