"""Configuration layer.

All settings have safe local defaults; every value can be overridden through
an environment variable prefixed with ``TPLAN_``. No secrets are involved --
the service is fully local and uses synthetic fixtures only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default) or default


@dataclass(frozen=True)
class Settings:
    db_path: str
    default_node_budget: int
    default_time_budget_seconds: float
    max_horizon: int
    max_actions: int
    max_repeats: int
    log_dir: str
    service_name: str = "tplan"
    version: str = "1.0.0"

    @staticmethod
    def from_env() -> "Settings":
        base = Path(__file__).resolve().parent.parent
        return Settings(
            db_path=_env_str("TPLAN_DB_PATH", str(base / "data" / "evidence.sqlite3")),
            default_node_budget=_env_int("TPLAN_NODE_BUDGET", 100_000),
            default_time_budget_seconds=float(_env_int("TPLAN_TIME_BUDGET_SECONDS", 10)),
            max_horizon=_env_int("TPLAN_MAX_HORIZON", 200),
            max_actions=_env_int("TPLAN_MAX_ACTIONS", 64),
            max_repeats=_env_int("TPLAN_MAX_REPEATS", 64),
            log_dir=_env_str("TPLAN_LOG_DIR", str(base / "logs")),
        )


SETTINGS = Settings.from_env()
