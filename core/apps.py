from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"
    verbose_name = "FieldSnap 核心"

    def ready(self):
        # 注册信号（创建用户时自动建立 Profile）
        from . import signals  # noqa: F401
