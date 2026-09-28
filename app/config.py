"""Application configuration layer.

Configuration is an explicit frozen object, not ambient globals:
:func:`load_settings` reads environment variables once at startup and
validates them. Values used inside planning requests (budgets, caps)
arrive per-request instead, so a wrong config can never silently turn a
query into a fixed answer.
"""
from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path

from app import __version__


def _env_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"environment variable {name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"environment variable {name} must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class Settings:
    app_name: str = "temporal-planner"
    version: str = __version__
    db_path: str = "data/planner.db"
    log_level: str = "INFO"
    default_budget_nodes: int = 200_000
    default_max_steps: int = 32
    sqlite_foreign_keys: bool = True
    python_version: str = field(default_factory=lambda: platform.python_version())
    platform_info: str = field(default_factory=lambda: platform.platform())

    def redacted_dict(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "version": self.version,
            "db_path": self.db_path,
            "log_level": self.log_level,
            "default_budget_nodes": self.default_budget_nodes,
            "default_max_steps": self.default_max_steps,
            "python_version": self.python_version,
            "platform": self.platform_info,
        }


def load_settings(overrides: dict[str, object] | None = None) -> Settings:
    """Build settings from environment, with optional explicit overrides.

    ``overrides`` (used by tests) take precedence over environment vars.
    """
    values: dict[str, object] = {
        "db_path": os.environ.get("PLANNER_DB_PATH", "data/planner.db"),
        "log_level": os.environ.get("PLANNER_LOG_LEVEL", "INFO").upper(),
        "default_budget_nodes": _env_int("PLANNER_DEFAULT_BUDGET", 200_000, minimum=1),
        "default_max_steps": _env_int("PLANNER_DEFAULT_MAX_STEPS", 32, minimum=1),
    }
    if overrides:
        values.update(overrides)
    db_path = str(values["db_path"])
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    if values["log_level"] not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ValueError(f"invalid PLANNER_LOG_LEVEL {values['log_level']!r}")
    return Settings(
        db_path=db_path,
        log_level=str(values["log_level"]),
        default_budget_nodes=int(values["default_budget_nodes"]),
        default_max_steps=int(values["default_max_steps"]),
    )


def runtime_versions() -> dict[str, str]:
    import fastapi
    import pydantic

    return {
        "service": __version__,
        "python": sys.version.split()[0],
        "fastapi": fastapi.__version__,
        "pydantic": pydantic.VERSION,
    }
