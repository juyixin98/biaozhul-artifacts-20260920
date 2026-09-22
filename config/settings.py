import os
import sys

import pymysql

# 以纯 Python 驱动模拟 MySQLdb，避免编译 mysqlclient 的系统依赖。
pymysql.install_as_MySQLdb()

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-in-production")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.admin",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "engine",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# 默认 MySQL（docker compose）；测试/本地轻量运行可用 DB_ENGINE=sqlite
if os.environ.get("DB_ENGINE", "mysql") == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.environ.get("SQLITE_PATH", str(BASE_DIR / "db.sqlite3")),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": os.environ.get("MYSQL_DATABASE", "text_engine"),
            "USER": os.environ.get("MYSQL_USER", "engine"),
            "PASSWORD": os.environ.get("MYSQL_PASSWORD", "engine_password"),
            "HOST": os.environ.get("MYSQL_HOST", "127.0.0.1"),
            "PORT": os.environ.get("MYSQL_PORT", "3306"),
            "OPTIONS": {"charset": "utf8mb4"},
        }
    }

AUTH_PASSWORD_VALIDATORS = []
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}

# ---- 业务参数 ----
BATCH_MAX_FILES = 100
FILE_MAX_BYTES = 10 * 1024 * 1024

# ---- 任务队列参数 ----
TASK_LEASE_SECONDS = int(os.environ.get("TASK_LEASE_SECONDS", "60"))
TASK_MAX_ATTEMPTS = int(os.environ.get("TASK_MAX_ATTEMPTS", "3"))
TASK_RETRY_BACKOFF_BASE = float(os.environ.get("TASK_RETRY_BACKOFF_BASE", "5"))
TASK_POLL_INTERVAL = float(os.environ.get("TASK_POLL_INTERVAL", "2"))

# ---- NLP ----
# 容器内模型随镜像安装；缺模型时引擎退化为内置正则分词（仅用于无模型环境/测试）。
SPACY_MODEL = os.environ.get("SPACY_MODEL", "en_core_web_sm")
_VENDOR_NLTK = BASE_DIR / "offline" / "nltk_data"
if _VENDOR_NLTK.exists():
    import nltk

    nltk.data.path.insert(0, str(_VENDOR_NLTK))
