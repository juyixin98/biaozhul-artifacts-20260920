"""Configuration layer.

All tunables are resolved here (env -> defaults) and exposed via an immutable
:class:`Settings` object. Nothing else in the codebase reads environment
variables directly, which keeps tests deterministic and review easy.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_DB_PATH = ":memory:"
DEFAULT_MAX_FIRE_ROUNDS = 100
DEFAULT_AGENDA_MODE = "manual"  # "manual" | "step" | "auto"
VALID_AGENDA_MODES = ("manual", "step", "auto")


class ConfigError(ValueError):
    """Raised when configuration values are missing or malformed."""


def _as_positive_int(name: str, raw: str | None, default: int) -> int:
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be positive, got {value}")
    return value


@dataclass(frozen=True)
class Settings:
    """Resolved engine configuration."""

    db_path: str
    max_fire_rounds: int
    agenda_mode: str
    log_level: str

    def describe(self) -> dict[str, object]:
        return {
            "db_path": self.db_path,
            "max_fire_rounds": self.max_fire_rounds,
            "agenda_mode": self.agenda_mode,
            "log_level": self.log_level,
        }


def load_settings(
    env: dict[str, str] | None = None,
    *,
    db_path: str | None = None,
    max_fire_rounds: int | None = None,
    agenda_mode: str | None = None,
    log_level: str | None = None,
) -> Settings:
    """Build settings from explicit kwargs first, then environment, then defaults.

    Explicit keyword arguments win over environment so tests can pin values
    without mutating ``os.environ``.
    """

    source = os.environ if env is None else env

    resolved_db = db_path if db_path is not None else source.get("RETE_DB_PATH", DEFAULT_DB_PATH)
    resolved_rounds = max_fire_rounds
    if resolved_rounds is None:
        resolved_rounds = _as_positive_int(
            "RETE_MAX_FIRE_ROUNDS", source.get("RETE_MAX_FIRE_ROUNDS"), DEFAULT_MAX_FIRE_ROUNDS
        )
    resolved_mode = agenda_mode if agenda_mode is not None else source.get("RETE_AGENDA_MODE", DEFAULT_AGENDA_MODE)
    if resolved_mode not in VALID_AGENDA_MODES:
        raise ConfigError(
            f"RETE_AGENDA_MODE must be one of {VALID_AGENDA_MODES}, got {resolved_mode!r}"
        )
    resolved_level = (log_level or source.get("RETE_LOG_LEVEL", "INFO")).upper()
    return Settings(
        db_path=resolved_db,
        max_fire_rounds=resolved_rounds,
        agenda_mode=resolved_mode,
        log_level=resolved_level,
    )
