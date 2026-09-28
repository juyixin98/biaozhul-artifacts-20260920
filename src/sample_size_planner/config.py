"""Independent configuration layer.

Everything that can vary between a local run and another deployment lives
here. Values are sourced from environment variables with safe local defaults;
no secret material is required for the synthetic/local workflow.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw is not None and raw.strip() else default


@dataclass(frozen=True)
class Settings:
    """Runtime settings (immutable; override via environment variables)."""

    host: str = os.environ.get("SSP_HOST", "127.0.0.1")
    port: int = _env_int("SSP_PORT", 8000)
    db_path: Path = Path(os.environ.get("SSP_DB_PATH", PROJECT_ROOT / "data" / "planner.db"))
    runs_dir: Path = Path(os.environ.get("SSP_RUNS_DIR", PROJECT_ROOT / "logs" / "runs"))
    log_dir: Path = Path(os.environ.get("SSP_LOG_DIR", PROJECT_ROOT / "logs"))
    fixture_dir: Path = Path(os.environ.get("SSP_FIXTURE_DIR", PROJECT_ROOT / "data"))

    # Estimation guards
    low_rate_threshold: float = float(os.environ.get("SSP_LOW_RATE_THRESHOLD", "5.0"))
    max_sample_size: int = _env_int("SSP_MAX_SAMPLE_SIZE", 10_000_000)
    mc_size_max: int = _env_int("SSP_MC_SIZE_MAX", 2_000_000)

    # Simulation defaults
    sim_default_replications: int = _env_int("SSP_SIM_REPLICATIONS", 20_000)
    sim_seed: int = _env_int("SSP_SIM_SEED", 20260927)
    sim_tolerance: float = float(os.environ.get("SSP_SIM_TOLERANCE", "0.02"))

    # Observing process
    max_interim_looks: int = _env_int("SSP_MAX_INTERIM_LOOKS", 20)

    verbose_logging: bool = _env_bool("SSP_VERBOSE", True)

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
