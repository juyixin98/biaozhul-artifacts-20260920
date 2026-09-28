"""Solver configuration: fixed segmentation, restart, and budget rules."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class SolverConfig:
    """Numerical and resource parameters for the Krylov exp(tA)v solver.

    Segmentation rule (fixed): the interval [0, t] is split into
    ``ceil(|t| * ||A||_1 / segment_theta)`` segments, clamped to
    ``[1, max_segments]``.

    Restart rule (fixed): a segment whose error estimate exceeds the
    tolerance is halved and retried, at most ``max_restart_splits``
    times; after that the request is reported as NOT_CONVERGED.
    """

    max_krylov_dim: int = 30
    segment_theta: float = 1.0
    max_segments: int = 4096
    max_restart_splits: int = 6
    default_tol: float = 1e-8
    breakdown_tol: float = 1e-14
    quadrature_points: int = 8
    max_dimension: int = 20000
    max_basis_bytes: int = 256 * 1024 * 1024

    @classmethod
    def from_env(cls, prefix: str = "KRYLOV_EXPM_") -> "SolverConfig":
        overrides = {}
        for name, default in asdict(cls()).items():
            raw = os.environ.get(prefix + name.upper())
            if raw is not None:
                overrides[name] = type(default)(raw)
        return cls(**overrides)

    def snapshot(self) -> dict:
        return asdict(self)
