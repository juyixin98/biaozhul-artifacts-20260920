"""保证系统账户归属的保留用户 SYSTEM 存在（不可登录、不可删除）。"""
from django.db import migrations


def create_system_user(apps, schema_editor):
    User = apps.get_model("auth", "User")
    if not User.objects.filter(username="SYSTEM").exists():
        User.objects.create(
            username="SYSTEM",
            is_active=False,
            is_staff=False,
            is_superuser=False,
        )


def remove_system_user(apps, schema_editor):
    User = apps.get_model("auth", "User")
    User.objects.filter(username="SYSTEM").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0001_initial"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(create_system_user, remove_system_user),
    ]
