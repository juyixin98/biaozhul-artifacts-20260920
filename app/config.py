"""运行期配置。全部可用环境变量覆盖，默认开箱即用。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    db_url: str = os.environ.get("KEX_DB_URL", "sqlite:////data/kex.db")
    admin_key: str = os.environ.get("KEX_ADMIN_KEY", "dev-admin-key")
    max_workers: int = int(os.environ.get("KEX_MAX_WORKERS", "2"))
    lease_seconds: int = int(os.environ.get("KEX_LEASE_SECONDS", "30"))
    max_attempts: int = int(os.environ.get("KEX_MAX_ATTEMPTS", "5"))
    enable_worker_threads: bool = os.environ.get("KEX_ENABLE_WORKER_THREADS", "1") != "0"
    builtin_rules_dir: str = os.environ.get("KEX_BUILTIN_RULES_DIR", os.path.join(os.path.dirname(__file__), "..", "rules"))
    migrations_dir: str = os.environ.get("KEX_MIGRATIONS_DIR", os.path.join(os.path.dirname(__file__), "..", "migrations"))
    http_host: str = os.environ.get("KEX_HTTP_HOST", "0.0.0.0")
    http_port: int = int(os.environ.get("KEX_HTTP_PORT", "8080"))


config = Config()
