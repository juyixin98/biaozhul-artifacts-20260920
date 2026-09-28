"""Configuration layer.

All knobs are environment driven so the service is reproducible without
editing source.  Only ``TINY_N_EXPLICIT`` is a semantic default: below this
size the engine routes through the explicitly-correct tiny path (an FFT of
size n is still computed there, not a hard-coded answer) unless the caller
forces the embedding kernel.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {raw!r}")
    return value


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings (see coding-style: no mutation)."""

    max_n: int
    max_batch: int
    cache_size: int
    rtol_bits: float
    # Problems strictly smaller than this use the explainable tiny path.
    tiny_n_explicit: int
    # mpmath precision for the high-precision oracle.
    oracle_mpmath_prec: int
    # n <= this for oracle cases may use dense O(n^2) numpy reference.
    dense_oracle_max_n: int
    log_level: str

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            max_n=_env_int("TOEPLITZ_MAX_N", 65_536),
            max_batch=_env_int("TOEPLITZ_MAX_BATCH", 256),
            cache_size=_env_int("TOEPLITZ_CACHE_SIZE", 128),
            # Default tolerance: ~4 * unit roundoff slack per element,
            # scaled per problem in evidence via condition-aware factors.
            rtol_bits=_env_float("TOEPLITZ_RTOL_BITS", 64.0),
            tiny_n_explicit=_env_int("TOEPLITZ_TINY_N", 2),
            oracle_mpmath_prec=_env_int("TOEPLITZ_ORACLE_PREC", 80),
            dense_oracle_max_n=_env_int("TOEPLITZ_DENSE_ORACLE_MAX_N", 512),
            log_level=os.environ.get("TOEPLITZ_LOG_LEVEL", "INFO").upper(),
        )


SETTINGS = Settings.from_env()
