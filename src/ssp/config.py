"""Environment-driven configuration.

All knobs are read from environment variables prefixed ``SSP_`` (see
``.env.example``).  Configuration is explicit: nothing depends on a production
account, and every path resolves under the project directory by default.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _project_root() -> Path:
    # src/ssp/config.py -> project root is three parents up.
    return Path(__file__).resolve().parents[2]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - defensive boundary
        raise ValueError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """Immutable process settings."""

    project_root: Path
    db_path: Path
    log_level: str
    log_dir: Path
    exact_one_sample_cap: int
    exact_two_sample_total_cap: int
    approx_min_expected: float
    mc_default_trials: int

    @classmethod
    def from_env(cls) -> "Settings":
        root = _project_root()
        db_raw = os.environ.get("SSP_DB_PATH", "data/plans.db")
        log_dir_raw = os.environ.get("SSP_LOG_DIR", "logs")
        db_path = Path(db_raw)
        log_dir = Path(log_dir_raw)
        if not db_path.is_absolute():
            db_path = root / db_path
        if not log_dir.is_absolute():
            log_dir = root / log_dir
        return cls(
            project_root=root,
            db_path=db_path,
            log_level=os.environ.get("SSP_LOG_LEVEL", "INFO").upper(),
            log_dir=log_dir,
            exact_one_sample_cap=_env_int("SSP_EXACT_ONE_SAMPLE_CAP", 200_000),
            exact_two_sample_total_cap=_env_int("SSP_EXACT_TWO_SAMPLE_TOTAL_CAP", 4_000),
            approx_min_expected=float(os.environ.get("SSP_APPROX_MIN_EXPECTED", "5")),
            mc_default_trials=_env_int("SSP_MC_DEFAULT_TRIALS", 8_000),
        )


def get_settings() -> Settings:
    """Single per-process settings object."""
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = Settings.from_env()
    return _SETTINGS


_SETTINGS: Settings | None = None


def reset_settings() -> None:
    """Drop the cached settings (tests re-point SSP_* env vars)."""
    global _SETTINGS
    _SETTINGS = None
