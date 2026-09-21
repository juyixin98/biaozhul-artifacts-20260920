"""RevStream settings.

Database selection is driven by the DB_ENGINE env var:
  DB_ENGINE=mysql (default inside docker) -> MySQL via mysqlclient
  otherwise                               -> local SQLite file

Everything else is configurable through environment variables so the same
image runs in docker compose and in CI.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(int(default))).lower() in {"1", "true", "yes", "on"}


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure-secret-key")
DEBUG = _env_bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = os.environ.get(
    "DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,0.0.0.0"
).split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "apps.accounts",
    "apps.catalog",
    "apps.ingestion",
    "apps.scoring",
    "apps.experiments",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "revstream.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "revstream.wsgi.application"

if os.environ.get("DB_ENGINE", "").lower() == "mysql":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": os.environ.get("MYSQL_DATABASE", "revstream"),
            "USER": os.environ.get("MYSQL_USER", "revstream"),
            "PASSWORD": os.environ.get("MYSQL_PASSWORD", ""),
            "HOST": os.environ.get("MYSQL_HOST", "db"),
            "PORT": os.environ.get("MYSQL_PORT", "3306"),
            "OPTIONS": {
                "charset": "utf8mb4",
                "init_command": "SET sql_mode='STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION'",
            },
            "CONN_MAX_AGE": 60,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "UNAUTHENTICATED_USER": None,
    "EXCEPTION_HANDLER": "apps.common.exceptions.revstream_exception_handler",
}

# ---------------------------------------------------------------------------
# Domain limits / business constants (single source of truth, documented in
# README.md).
# ---------------------------------------------------------------------------
MAX_NETWORKS_PER_PLACEMENT = 8
EVENT_BATCH_MAX_SIZE = 2000
SCORING_WINDOW_DAYS = 7
SCORING_PERIOD_MINUTES = 30
SCORE_WEIGHT_FILL_RATE = 0.40
SCORE_WEIGHT_ECPM = 0.35
SCORE_WEIGHT_RELIABILITY = 0.25
# Events older than this relative to "now" are rejected on ingestion.
EVENT_MAX_AGE_DAYS = 30
# Events more than this far in the future are rejected (clock skew guard).
EVENT_MAX_FUTURE_MINUTES = 5

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}
