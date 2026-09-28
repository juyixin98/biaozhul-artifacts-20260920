"""Configuration layer for the sparse Cholesky backend.

Everything tunable lives here so the computation kernels and the service layer
do not carry magic numbers. Values can be overridden through environment
variables (prefix ``SPCHOL_``) which keeps the service configurable without
code edits.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class FactorizationConfig:
    """Numerical tolerances and limits for the numeric kernels."""

    #: Pivots with magnitude below this (absolute) are reported as non-positive.
    pivot_tol_abs: float = 1e-14
    #: Pivot smaller than ``pivot_tol_rel * |a00|`` is treated as failure.
    pivot_tol_rel: float = 1e-14
    #: Minimum pivot allowed when the input is asserted positive definite.
    min_pivot: float = 0.0
    #: High precision (mpmath) digits used when gathering error evidence.
    mpmath_dps: int = 50
    #: Maximum matrix order accepted by the service layer (defensive bound).
    max_order: int = 20_000
    #: Largest nnz(A) accepted, guards against malformed huge payloads.
    max_nnz: int = 2_000_000
    #: Whether numeric factorizations may reuse a cached symbolic structure.
    symbolic_cache_enabled: bool = True


@dataclass(frozen=True)
class ServerConfig:
    """Service-layer settings."""

    host: str = field(default_factory=lambda: os.getenv("SPCHOL_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("SPCHOL_PORT", 8000))
    log_dir: str = field(
        default_factory=lambda: os.getenv("SPCHOL_LOG_DIR", "logs")
    )
    #: Include dense arrays in JSON responses (off by default: stays sparse).
    response_include_arrays: bool = field(
        default_factory=lambda: _env_bool("SPCHOL_INCLUDE_ARRAYS", False)
    )


@lru_cache(maxsize=1)
def get_factorization_config() -> FactorizationConfig:
    """Return the factorization config, populated from the environment."""
    return FactorizationConfig(
        pivot_tol_abs=_env_float("SPCHOL_PIVOT_TOL_ABS", 1e-14),
        pivot_tol_rel=_env_float("SPCHOL_PIVOT_TOL_REL", 1e-14),
        min_pivot=0.0,
        mpmath_dps=_env_int("SPCHOL_MPMATH_DPS", 50),
        max_order=_env_int("SPCHOL_MAX_ORDER", 20_000),
        max_nnz=_env_int("SPCHOL_MAX_NNZ", 2_000_000),
        symbolic_cache_enabled=_env_bool("SPCHOL_SYMBOLIC_CACHE", True),
    )


@lru_cache(maxsize=1)
def get_server_config() -> ServerConfig:
    return ServerConfig()


def reset_config_cache() -> None:
    """Drop cached configs (used by tests that mutate the environment)."""
    get_factorization_config.cache_clear()
    get_server_config.cache_clear()
