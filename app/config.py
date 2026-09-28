"""Application configuration.

All tunable thresholds live here so that numerical kernels never contain
magic numbers. Values can be overridden through environment variables.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _as_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw is not None else default


def _as_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw is not None else default


@dataclass(frozen=True)
class Settings:
    # Numerical tolerances ------------------------------------------------
    # Relative pivot floor: a pivot |d| <= pivot_tol * scale is treated as
    # non-positive. ``scale`` is the largest absolute diagonal seen so far.
    pivot_tol: float = 1e-12
    # Residual tests above this multiple of machine precision are reported
    # (the decomposition itself is still returned; only evidence flags it).
    residual_warn_factor: float = 1e-8
    # Symmetry is checked in floating point: |A_ij - A_ji| <= sym_tol * ...
    symmetry_tol: float = 1e-10

    # Sparse kernel limits ------------------------------------------------
    max_dimension: int = 20000
    max_nnz_upper: int = 2_000_000

    # mpmath reference precision (decimal digits)
    mpmath_dps: int = 50

    # Service --------------------------------------------------------------
    cache_max_entries: int = 128
    log_dir: str = "logs"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            pivot_tol=_as_float("SPD_PIVOT_TOL", cls.pivot_tol),
            residual_warn_factor=_as_float("SPD_RESIDUAL_WARN_FACTOR",
                                           cls.residual_warn_factor),
            symmetry_tol=_as_float("SPD_SYMMETRY_TOL", cls.symmetry_tol),
            max_dimension=_as_int("SPD_MAX_DIMENSION", cls.max_dimension),
            max_nnz_upper=_as_int("SPD_MAX_NNZ_UPPER", cls.max_nnz_upper),
            mpmath_dps=_as_int("SPD_MPMATH_DPS", cls.mpmath_dps),
            cache_max_entries=_as_int("SPD_CACHE_MAX_ENTRIES",
                                      cls.cache_max_entries),
            log_dir=os.environ.get("SPD_LOG_DIR", cls.log_dir),
        )


settings = Settings.from_env()
