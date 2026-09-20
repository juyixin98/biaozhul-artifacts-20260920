"""
Django settings for the MLOps local resource scheduling engine.

数据库默认使用 MySQL（docker compose 提供），可通过 DB_ENGINE=sqlite
切换到 SQLite，方便在没有 MySQL 的环境跑单元测试。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes", "on")


SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY", "dev-insecure-key-replace-in-production-9f8a2b"
)
DEBUG = _env_bool("DJANGO_DEBUG", "true")
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.staticfiles",
    "rest_framework",
    "scheduler",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
if os.environ.get("DB_ENGINE", "mysql") == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.environ.get("SQLITE_NAME", str(BASE_DIR / "db.sqlite3")),
            # SQLite 单连接串行化，测试中并发逻辑由线程配合内存锁验证。
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": os.environ.get("MYSQL_DATABASE", "mlops_scheduler"),
            "USER": os.environ.get("MYSQL_USER", "scheduler"),
            "PASSWORD": os.environ.get("MYSQL_PASSWORD", "scheduler_pw"),
            "HOST": os.environ.get("MYSQL_HOST", "127.0.0.1"),
            "PORT": os.environ.get("MYSQL_PORT", "3306"),
            # MySQL 8 默认 caching_sha2_password；读已提交兼顾并发与一致性。
            "OPTIONS": {
                "charset": "utf8mb4",
                "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
                "isolation_level": "read committed",
            },
            "CONN_MAX_AGE": int(os.environ.get("DB_CONN_MAX_AGE", "60")),
            "TEST": {"CHARSET": "utf8mb4", "COLLATION": "utf8mb4_unicode_ci"},
        }
    }

# ---------------------------------------------------------------------------
# 调度引擎相关可调参数（均有默认值，可被环境变量覆盖）
# ---------------------------------------------------------------------------
SCHEDULER = {
    # 心跳超过该秒数没有上报的节点判定为离线（失联）。
    "HEARTBEAT_TIMEOUT_SECONDS": int(os.environ.get("HEARTBEAT_TIMEOUT_SECONDS", 30)),
    # 排队超过该秒数的作业自动取消（需求：2 小时）。
    "QUEUE_TIMEOUT_SECONDS": int(os.environ.get("QUEUE_TIMEOUT_SECONDS", 2 * 3600)),
    # 运行中的作业超过其 run_timeout_seconds 后判超时取消；0 表示不限制。
    "DEFAULT_RUN_TIMEOUT_SECONDS": int(
        os.environ.get("DEFAULT_RUN_TIMEOUT_SECONDS", 6 * 3600)
    ),
    # 允许抢占的优先级边界：priority >= PREEMPTION_PRIORITY 的作业
    # 可以抢占 priority <= PREEMPTIBLE_PRIORITY 的作业。
    "PREEMPTION_PRIORITY": 8,
    "PREEMPTIBLE_PRIORITY": 3,
}

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
}
