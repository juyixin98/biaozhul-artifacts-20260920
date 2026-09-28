"""Application configuration.

All knobs are environment-overridable; defaults are sized for the local
synthetic-data service. Nothing here is a secret.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw is not None else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw is not None else default


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    """Service-wide settings (frozen: pass a modified copy, never mutate)."""

    # Database for run/evidence persistence
    database_path: Path = field(
        default_factory=lambda: Path(_env_str("TWOSLS_DB_PATH", str(PROJECT_ROOT / "data" / "twosls.db")))
    )

    # Request size guards (system-boundary input validation)
    max_observations: int = field(default_factory=lambda: _env_int("TWOSLS_MAX_N", 100_000))
    min_observations: int = field(default_factory=lambda: _env_int("TWOSLS_MIN_N", 10))
    max_columns: int = field(default_factory=lambda: _env_int("TWOSLS_MAX_COLS", 200))

    # Numerical tolerances
    rank_tol: float = field(default_factory=lambda: _env_float("TWOSLS_RANK_TOL", 1e-10))
    collinear_corr: float = field(default_factory=lambda: _env_float("TWOSLS_COLLINEAR_CORR", 0.999))

    # Weak-instrument thresholds (Stock-Yogo style; documented in README)
    weak_f_threshold: float = field(default_factory=lambda: _env_float("TWOSLS_WEAK_F", 10.0))
    partial_r2_low: float = field(default_factory=lambda: _env_float("TWOSLS_PARTIAL_R2_LOW", 0.10))

    # Diagnostics
    sargan_significance: float = field(default_factory=lambda: _env_float("TWOSLS_SARGAN_ALPHA", 0.05))
    endogeneity_significance: float = field(default_factory=lambda: _env_float("TWOSLS_DURBIN_ALPHA", 0.05))

    # Logging
    log_level: str = field(default_factory=lambda: _env_str("TWOSLS_LOG_LEVEL", "INFO"))

    # Whether request payload samples may be echoed into logs (always redacted
    # by default; set TWOSLS_LOG_PAYLOAD=1 only for local debugging).
    log_payload: bool = field(default_factory=lambda: _env_str("TWOSLS_LOG_PAYLOAD", "0") == "1")


settings = Settings()
