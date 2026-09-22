"""Django settings for the local text analysis engine."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = [h for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",") if h]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt",
    "analysis",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

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

STATIC_URL = "static/"

ROOT_URLCONF = "textengine.urls"
WSGI_APPLICATION = "textengine.wsgi.application"

# MySQL by default (docker compose). USE_SQLITE=1 / missing mysqlclient falls
# back to SQLite for quick local logic runs and the non-concurrent test suite.
_USE_SQLITE = os.environ.get("USE_SQLITE", "") == "1"
if not _USE_SQLITE:
    try:
        import MySQLdb  # noqa: F401
    except ImportError:
        _USE_SQLITE = True

if _USE_SQLITE:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": os.environ.get("MYSQL_DATABASE", "textengine"),
            "USER": os.environ.get("MYSQL_USER", "textengine"),
            "PASSWORD": os.environ.get("MYSQL_PASSWORD", "textengine-pass"),
            "HOST": os.environ.get("MYSQL_HOST", "127.0.0.1"),
            "PORT": os.environ.get("MYSQL_PORT", "3306"),
            "OPTIONS": {"charset": "utf8mb4"},
            "CONN_MAX_AGE": 60,
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_TZ = True

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
}

# ---- Analysis engine configuration -----------------------------------------
ANALYSIS = {
    # Bumped whenever metric formulas change; results carry the version used
    # to produce them and a rerun always creates a new result row.
    "ALGORITHM_VERSION": "1.0.0",
    "MAX_BATCH_FILES": 100,
    "MAX_FILE_BYTES": 10 * 1024 * 1024,
    # Style comparison needs at least this many OTHER already-analysed
    # documents in the same course; below it the metric is reported as
    # unavailable rather than producing a meaningless number.
    "STYLE_MIN_REFERENCE_SAMPLES": 3,
    # Repeated-fragment detector: repeated n-gram of token n-grams...
    "REPEAT_NGRAM_RANGE": (8, 12, 16),
}

# ---- Queue configuration ---------------------------------------------------
QUEUE = {
    "LEASE_SECONDS": int(os.environ.get("WORKER_LEASE_SECONDS", "120")),
    "HEARTBEAT_SECONDS": int(os.environ.get("WORKER_HEARTBEAT_SECONDS", "30")),
    "MAX_ATTEMPTS": 3,
    "POLL_INTERVAL": float(os.environ.get("WORKER_POLL_INTERVAL", "2")),
}

# NLTK looks here first; the Docker image points it at the vendored data.
_NLTK_DIR = str(BASE_DIR / "vendor" / "nltk_data")
if os.path.isdir(_NLTK_DIR):
    os.environ.setdefault("NLTK_DATA", _NLTK_DIR)
