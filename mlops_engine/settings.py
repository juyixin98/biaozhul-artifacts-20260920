"""
Django settings for the MLOps local resource scheduling engine.

Only GPU resources and job states are simulated — there is no real training
and no recommendation system.

Database selection is environment driven so the same code runs against MySQL
(docker compose) or SQLite (local unit tests / quick experiments):

    DB_ENGINE=sqlite   (default outside docker; uses ML_SCHED_DB or a file)
    DB_ENGINE=mysql    (used inside docker compose)
"""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "insecure-local-dev-key-change-in-production",
)
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "scheduler",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "mlops_engine.urls"

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
            ]
        },
    },
]

WSGI_APPLICATION = "mlops_engine.wsgi.application"

DB_ENGINE = os.environ.get("DB_ENGINE", "sqlite")
if DB_ENGINE == "mysql":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": os.environ.get("DB_NAME", "mlops"),
            "USER": os.environ.get("DB_USER", "mlops"),
            "PASSWORD": os.environ.get("DB_PASSWORD", "mlops_password"),
            "HOST": os.environ.get("DB_HOST", "db"),
            "PORT": os.environ.get("DB_PORT", "3306"),
            "OPTIONS": {"charset": "utf8mb4"},
            "CONN_MAX_AGE": int(os.environ.get("DB_CONN_MAX_AGE", 0)),
        }
    }
else:
    # File-based by default so concurrent threads/processes share the DB in
    # tests; override with ML_SCHED_DB=/tmp/xxx.sqlite3
    sqlite_path = os.environ.get("ML_SCHED_DB", os.path.join(BASE_DIR, "db.sqlite3"))
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": sqlite_path,
            # Keep short-transaction locks from exploding under concurrency.
            "OPTIONS": {"timeout": 30},
            "TEST": {"NAME": os.environ.get("ML_SCHED_TEST_DB", sqlite_path)},
        }
    }

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Scheduler tuning (all overridable via environment / fake clock in tests)
# ---------------------------------------------------------------------------
SCHEDULER = {
    # A node whose last heartbeat is older than this is treated as OFFLINE.
    "HEARTBEAT_TIMEOUT_SECONDS": int(os.environ.get("HEARTBEAT_TIMEOUT_SECONDS", 30)),
    # A PENDING job older than this is cancelled with reason TIMEOUT (2h spec).
    "QUEUE_TIMEOUT_SECONDS": int(os.environ.get("QUEUE_TIMEOUT_SECONDS", 60 * 60 * 2)),
    # A worker must release a preempted job within this window or the
    # scheduler force-settles the victim.
    "PREEMPT_GRACE_SECONDS": int(os.environ.get("PREEMPT_GRACE_SECONDS", 30)),
}

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "UNAUTHENTICATED_USER": None,
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
}
