"""公共测试夹具。"""
from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from core.models import FormTemplate, Profile, Project, Team

User = get_user_model()

SCHEMA_V1 = {
    "fields": [
        {"key": "site_name", "label": "工地", "type": "text", "required": True},
        {"key": "phase", "label": "阶段", "type": "enum",
         "options": ["foundation", "framing", "finishing"]},
        {"key": "temp", "label": "温度", "type": "number", "min": -50, "max": 100},
        {"key": "day", "label": "日期", "type": "date", "required": True},
        {"key": "issue_note", "label": "异常说明", "type": "text",
         "required_if": {"field": "phase", "op": "eq", "value": "finishing"}},
    ]
}


class FieldSnapAPITestCase(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project = Project.objects.create(code="P1", name="一号项目")
        cls.other_project = Project.objects.create(code="P2", name="二号项目")

        cls.team = Team.objects.create(code="T1", name="一班")
        cls.team.projects.add(cls.project)
        cls.other_team = Team.objects.create(code="T2", name="二班")
        cls.other_team.projects.add(cls.other_project)

        cls.admin = User.objects.create_user("admin", password="x", is_staff=True)
        cls.sup = User.objects.create_user("sup", password="x")
        cls.sup2 = User.objects.create_user("sup2", password="x")
        cls.w1 = User.objects.create_user("w1", password="x")
        cls.w2 = User.objects.create_user("w2", password="x")
        cls.other_worker = User.objects.create_user("ow", password="x")

        cls.sup.profile.role = Profile.Role.SUPERVISOR
        cls.sup.profile.save()
        cls.sup2.profile.role = Profile.Role.SUPERVISOR
        cls.sup2.profile.save()
        cls.team.supervisors.add(cls.sup)
        cls.other_team.supervisors.add(cls.sup2)

        cls.team.members.add(cls.w1, cls.w2)
        cls.w1.profile.assigned_projects.add(cls.project)
        cls.w2.profile.assigned_projects.add(cls.project)
        cls.other_worker.profile.assigned_projects.add(cls.other_project)
        cls.other_team.members.add(cls.other_worker)

        for u in (cls.admin, cls.sup, cls.sup2, cls.w1, cls.w2, cls.other_worker):
            Token.objects.create(user=u)

        cls.template = FormTemplate.objects.create(
            project=cls.project,
            code="site_check",
            version=1,
            name="巡检表",
            schema=SCHEMA_V1,
            status=FormTemplate.Status.PUBLISHED,
            created_by=cls.admin,
        )
        cls.other_template = FormTemplate.objects.create(
            project=cls.other_project,
            code="site_check",
            version=1,
            name="巡检表",
            schema=SCHEMA_V1,
            status=FormTemplate.Status.PUBLISHED,
            created_by=cls.admin,
        )

    def auth(self, user):
        token = Token.objects.get(user=user).key
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token}")

    def push(self, user, client_batch_id, items, project=None, device_id="dev"):
        self.auth(user)
        url = f"/api/projects/{(project or self.project).id}/sync/push/"
        body = {"client_batch_id": client_batch_id, "device_id": device_id,
                "items": items}
        return self.client.post(url, body, format="json")

    def pull_all(self, user, project=None, page_size=2):
        """完整走完一轮 hwm 快照的所有页，返回变更列表。"""
        self.auth(user)
        pid = (project or self.project).id
        first = self.client.get(f"/api/projects/{pid}/sync/pull/?limit={page_size}")
        assert first.status_code == 200, first.content
        data = first.json()
        changes = list(data["changes"])
        hwm = data["hwm"]
        while data["has_more"]:
            r = self.client.get(
                f"/api/projects/{pid}/sync/pull/"
                f"?cursor={data['next_cursor']}&hwm={hwm}&limit={page_size}"
            )
            assert r.status_code == 200, r.content
            data = r.json()
            changes.extend(data["changes"])
        return changes, hwm

    @staticmethod
    def item(uuid, data, record_version=1, *, template_version=1,
             collected_at="2026-09-21T08:00:00Z", template_code="site_check"):
        return {
            "uuid": str(uuid),
            "template_code": template_code,
            "template_version": template_version,
            "record_version": record_version,
            "collected_at": collected_at,
            "data": data,
        }

    @staticmethod
    def good_data(**overrides):
        data = {
            "site_name": "1号井",
            "phase": "foundation",
            "temp": 22,
            "day": "2026-09-21",
        }
        data.update(overrides)
        return data
