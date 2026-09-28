"""Numerical (unverified) root approximation via SciPy.

This layer is deliberately independent of the certification kernel: it finds
floating-point root *candidates* with a mesh scan + Brent's method. Results are
tagged ``approximate_unverified`` and must never be confused with certified
enclosures -- they are only hints for the human/operator and for checking
coverage of undecided regions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
from scipy.optimize import brentq

from .config import CertConfig
from .evaluator import Evaluator
from .errors import ComputationFailure

# Floating-point candidates closer than this (relative to the search width)
# are treated as the same candidate.
DEDUP_REL_TOL = 1e-10
BRENT_TOL = 1e-14


@dataclass(frozen=True)
class ApproximateRoot:
    value: float
    residual: float
    method: str
    converged: bool


@dataclass(frozen=True)
class ApproximationReport:
    roots: list[ApproximateRoot]
    sample_count: int
    convergence_failures: int


def approximate_roots(
    f_node,
    ev: Evaluator,
    lo: Any,
    hi: Any,
    config: CertConfig,
    sample_count: int = 1001,
) -> ApproximationReport:
    """Scan ``[lo, hi]`` for sign changes and refine each with Brent.

    A pure-Python ``f`` over the request's high-precision context is wrapped
    to a float callable. Even-multiplicity roots that do not change sign are
    invisible to a sign scan (documented limitation); they are hinted at
    separately by the certification layer's undecided regions.
    """
    if sample_count < 3:
        sample_count = 3

    def scalar_f(x_float: float) -> float:
        try:
            return float(ev.point_mp(f_node, ev.mp.mpf(float(x_float))))
        except Exception:  # domain points inside a scan are treated as gaps
            return np.nan

    lo_f, hi_f = float(lo), float(hi)
    grid = np.linspace(lo_f, hi_f, sample_count)
    values = np.array([scalar_f(x) for x in grid], dtype=float)

    roots: list[ApproximateRoot] = []
    failures = 0
    for i in range(len(grid) - 1):
        a, b = values[i], values[i + 1]
        if np.isnan(a) or np.isnan(b):
            continue
        if a == 0.0:
            candidate = _make_root(grid[i], scalar_f)
            roots = _add(roots, candidate, lo_f, hi_f)
            continue
        if a * b < 0:
            try:
                root = brentq(
                    scalar_f, grid[i], grid[i + 1],
                    xtol=BRENT_TOL, rtol=BRENT_TOL, maxiter=200,
                )
                roots = _add(
                    roots, _make_root(root, scalar_f), lo_f, hi_f
                )
            except (RuntimeError, ValueError) as exc:
                failures += 1
                raise ComputationFailure(
                    f"Brent refinement failed: {exc}",
                    {"bracket": [float(grid[i]), float(grid[i + 1])]},
                ) from exc
    # Catch a root exactly at the final grid point.
    if len(grid) and values[-1] == 0.0:
        roots = _add(roots, _make_root(grid[-1], scalar_f), lo_f, hi_f)

    return ApproximationReport(
        roots=roots,
        sample_count=sample_count,
        convergence_failures=failures,
    )


def _make_root(value: float, scalar_f: Callable[[float], float]) -> ApproximateRoot:
    return ApproximateRoot(
        value=float(value),
        residual=float(scalar_f(value)),
        method="scipy.optimize.brentq",
        converged=True,
    )


def _add(
    roots: list[ApproximateRoot],
    candidate: ApproximateRoot,
    lo: float,
    hi: float,
) -> list[ApproximateRoot]:
    span = max(abs(hi - lo), 1.0)
    for existing in roots:
        if abs(existing.value - candidate.value) < DEDUP_REL_TOL * span:
            return roots
    return roots + [candidate]
