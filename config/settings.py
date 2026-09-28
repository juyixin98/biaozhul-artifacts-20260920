"""Typed service configuration with explicit budgets.

Every limit that can make an exact-computation request blow up is represented
here rather than hidden as a magic number in the kernel. Values can be
overridden through environment variables (prefix ``RI_``) so deployments can
tighten or relax budgets without touching code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"environment variable {name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"environment variable {name} must be positive, got {value!r}")
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"environment variable {name} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"environment variable {name} must be positive, got {value!r}")
    return value


@dataclass(frozen=True)
class BudgetConfig:
    """Exact-arithmetic resource budgets.

    These bounds guard against adversarial polynomials (huge degree, enormous
    coefficients) whose exact Sturm computation would not terminate in useful
    time or memory.
    """

    max_degree: int = 64
    max_coefficient_bits: int = 4096
    max_sturm_pairs: int = 2_000_000
    max_bisection_depth: int = 200
    max_roots: int = 256


@dataclass(frozen=True)
class NumericConfig:
    """Settings for the independent high-precision cross-check."""

    mpmath_prec: int = 113
    verify_tolerance: float = 1e-24
    sample_grid_multiplier: int = 16


@dataclass(frozen=True)
class LogConfig:
    """Diagnostics settings.

    ``redact_polynomials`` causes logs to omit raw coefficient payloads so a
    request carrying sensitive input never leaks it verbatim.
    """

    level: str = "INFO"
    redact_polynomials: bool = True


@dataclass(frozen=True)
class ServiceConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    budget: BudgetConfig = None  # type: ignore[assignment]
    numeric: NumericConfig = None  # type: ignore[assignment]
    log: LogConfig = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        # frozen dataclass + object.__setattr__ keeps instances immutable while
        # allowing nested defaults built from environment overrides.
        if self.budget is None:
            object.__setattr__(self, "budget", load_budget())
        if self.numeric is None:
            object.__setattr__(self, "numeric", load_numeric())
        if self.log is None:
            object.__setattr__(self, "log", load_log())


def load_budget() -> BudgetConfig:
    return BudgetConfig(
        max_degree=_env_int("RI_MAX_DEGREE", 64),
        max_coefficient_bits=_env_int("RI_MAX_COEFFICIENT_BITS", 4096),
        max_sturm_pairs=_env_int("RI_MAX_STURM_PAIRS", 2_000_000),
        max_bisection_depth=_env_int("RI_MAX_BISECTION_DEPTH", 200),
        max_roots=_env_int("RI_MAX_ROOTS", 256),
    )


def load_numeric() -> NumericConfig:
    return NumericConfig(
        mpmath_prec=_env_int("RI_MPMATH_PREC", 113),
        verify_tolerance=_env_float("RI_VERIFY_TOL", 1e-24),
        sample_grid_multiplier=_env_int("RI_SAMPLE_GRID_MULTIPLIER", 16),
    )


def load_log() -> LogConfig:
    level = os.environ.get("RI_LOG_LEVEL", "INFO").strip().upper()
    redact = os.environ.get("RI_LOG_REDACT", "true").strip().lower() in {"1", "true", "yes", "on"}
    return LogConfig(level=level, redact_polynomials=redact)


def load_settings() -> ServiceConfig:
    """Build the full configuration from environment and defaults."""

    return ServiceConfig(
        host=os.environ.get("RI_HOST", "0.0.0.0"),
        port=_env_int("RI_PORT", 8000),
        budget=load_budget(),
        numeric=load_numeric(),
        log=load_log(),
    )
