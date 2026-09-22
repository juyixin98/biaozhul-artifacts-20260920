"""写入演示数据（幂等，可反复执行）。

账号（密码均为 fieldsnap123）：
  admin      管理员
  sup_north  北区主管
  w1 / w2    两名工作人员
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone
from rest_framework.authtoken.models import Token

from core.models import FormTemplate, Profile, Project, Team

User = get_user_model()

PASSWORD = "fieldsnap123"

SCHEMA_V1 = {
    "fields": [
        {"key": "site_name", "label": "工地名称", "type": "text", "required": True},
        {"key": "phase", "label": "施工阶段", "type": "enum",
         "options": ["foundation", "framing", "finishing"]},
        {"key": "temp", "label": "环境温度", "type": "number", "min": -50, "max": 100},
        {"key": "day", "label": "检查日期", "type": "date", "required": True},
        {"key": "issue_note", "label": "异常说明", "type": "text",
         "required_if": {"field": "phase", "op": "eq", "value": "finishing"}},
    ]
}


class Command(BaseCommand):
    help = "写入演示用户、班组、项目与已发布表单 v1"

    def handle(self, *args, **options):
        project, _ = Project.objects.get_or_create(
            code="NORTH-PJ", defaults={"name": "北区管网项目"}
        )
        team, _ = Team.objects.get_or_create(
            code="TEAM-A", defaults={"name": "北一班"}
        )
        team.projects.add(project)

        def ensure_user(username, role, is_staff=False):
            user = User.objects.filter(username=username).first()
            created = False
            if user is None:
                user = User.objects.create_user(
                    username=username, password=PASSWORD, is_staff=is_staff
                )
                created = True
            if hasattr(user, "profile"):
                user.profile.role = role
                user.profile.save()
            if not Token.objects.filter(user=user).exists():
                Token.objects.create(user=user)
            return user, created

        admin_u, admin_c = ensure_user("admin", Profile.Role.ADMIN, is_staff=True)
        sup, sup_c = ensure_user("sup_north", Profile.Role.SUPERVISOR)
        w1, w1c = ensure_user("w1", Profile.Role.WORKER)
        w2, w2c = ensure_user("w2", Profile.Role.WORKER)

        team.supervisors.add(sup)
        team.members.add(w1, w2)
        for u in (w1, w2):
            u.profile.assigned_projects.add(project)

        tpl = FormTemplate.objects.filter(
            project=project, code="site_check", version=1
        ).first()
        if tpl is None:
            tpl = FormTemplate.objects.create(
                project=project,
                code="site_check",
                version=1,
                name="现场巡检表",
                schema=SCHEMA_V1,
                status=FormTemplate.Status.PUBLISHED,
                created_by=admin_u,
                published_at=timezone.now(),
            )

        self.stdout.write(self.style.SUCCESS("演示数据已就绪："))
        for username, created in (
            ("admin", admin_c), ("sup_north", sup_c),
            ("w1", w1c), ("w2", w2c),
        ):
            token = Token.objects.get(user__username=username)
            self.stdout.write(
                f"  {username:10s} / {PASSWORD}  token={token.key}"
            )
        self.stdout.write(f"  项目={project.code}  模板={tpl.code} v{tpl.version}")
