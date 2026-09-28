"""Application configuration loaded from environment variables.

All tunables live here so the service can be driven purely from the
environment (12-factor style).  No secrets are involved; values are
validated eagerly at startup so a bad configuration fails fast.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# IEEE-754 binary64: rounding unit u = 2^-53.  Kept here (not in the kernels)
# because several independent modules share the constant.
MACHINE_EPSILON: float = 2.0 ** -53


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:  # pragma: no cover - defensive boundary
        raise ValueError(f"environment variable {name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"environment variable {name} must be positive, got {value}")
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover - defensive boundary
        raise ValueError(f"environment variable {name} must be a float, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings."""

    # Largest number of elements accepted in a single request body.
    max_input_length: int
    # Default block size used by the chunked merge endpoints.
    default_block_size: int
    # mpmath.mp.dps used for the high-precision reference sums.
    reference_precision: int
    # Requests smaller than this still get a bounded-precision reference;
    # above it we cap mpmath work and widen the error tolerance instead.
    reference_max_length: int
    # Multiplier on theoretical error bounds used to adjudicate verdicts.
    error_tolerance_factor: float
    # Service identity echoed into diagnostics / log records.
    service_name: str

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            max_input_length=_env_int("SUMAPI_MAX_INPUT_LENGTH", 5_000_000),
            default_block_size=_env_int("SUMAPI_DEFAULT_BLOCK_SIZE", 4096),
            reference_precision=_env_int("SUMAPI_REFERENCE_PRECISION", 80),
            reference_max_length=_env_int("SUMAPI_REFERENCE_MAX_LENGTH", 200_000),
            error_tolerance_factor=_env_float("SUMAPI_ERROR_TOLERANCE_FACTOR", 4.0),
            service_name=os.environ.get("SUMAPI_SERVICE_NAME", "summation-compare-api"),
        )


settings = Settings.from_env()
