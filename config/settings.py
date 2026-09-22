"""
Django settings for CryptoLaunch local simulated matching engine.

No blockchain, no real funds, no external market data.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY", "dev-insecure-key-change-me-in-production"
)
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "rest_framework",
    "rest_framework.authtoken",
    "apps.accounts",
    "apps.markets",
    "apps.trading",
    "apps.ledger",
    "apps.audit",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# ---------------------------------------------------------------------------
# Database
#
# Docker Compose uses MySQL.  Set DB_ENGINE=sqlite (or leave MYSQL_HOST unset)
# to run the test suite quickly without a MySQL server.  Concurrency tests are
# only meaningful on MySQL and skip themselves on sqlite.
# ---------------------------------------------------------------------------
if os.environ.get("DB_ENGINE", "mysql") == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.environ.get("SQLITE_PATH", str(BASE_DIR / "db.sqlite3")),
        }
    }
else:
    import pymysql

    pymysql.install_as_MySQLdb()

    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": os.environ.get("MYSQL_DATABASE", "cryptolaunch"),
            "USER": os.environ.get("MYSQL_USER", "cryptolaunch"),
            "PASSWORD": os.environ.get("MYSQL_PASSWORD", "cryptolaunch"),
            "HOST": os.environ.get("MYSQL_HOST", "127.0.0.1"),
            "PORT": os.environ.get("MYSQL_PORT", "3306"),
            "OPTIONS": {
                "charset": "utf8mb4",
                "init_command": (
                    "SET sql_mode='STRICT_TRANS_TABLES,ERROR_FOR_DIVISION_BY_ZERO';"
                ),
            },
            "CONN_MAX_AGE": int(os.environ.get("DB_CONN_MAX_AGE", "60")),
        }
    }

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.BasicAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_PAGINATION_CLASS":
        "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 50,
    "UNAUTHENTICATED_USER": None,
}

# ---------------------------------------------------------------------------
# Engine rules
# ---------------------------------------------------------------------------
# All money/quantity math is fixed point with at most 8 decimal places.
ENGINE_DECIMAL_PLACES = 8
# Fee account username -- fee ledger entries post to this user's balances.
FEE_ACCOUNT_USERNAME = os.environ.get("FEE_ACCOUNT_USERNAME", "fee-account")
# Account that holds simulated funds minted by the seed script.
SIM_BANK_USERNAME = os.environ.get("SIM_BANK_USERNAME", "sim-bank")
# Genesis equity account: simulated mints debit this account, so every
# ledger event still has balanced entries.  Its balance is the (negative)
# total simulated supply and is excluded from non-negative checks.
SIM_GENESIS_USERNAME = os.environ.get("SIM_GENESIS_USERNAME", "sim-genesis")

# Gunicorn is run with 1 worker + N threads so the process-local order book
# is always consistent.  The engine is still correct with multiple workers
# (each call rebuilds the book under a row lock), only slower.
ENGINE_MAINTENANCE_DEFAULT = False
