"""Drift metrics computed on aligned bucket probability vectors.

All metrics take two non-negative, same-length count/probability vectors
(see :mod:`drift_monitor.binning`). PSI is mathematically symmetric in the
two windows (``PSI(p, q) == PSI(q, p)``); the baseline/current argument
order only determines which smoothed counts are reported as which side.

Smoothing
---------
PSI/JS are undefined (infinite) when a bucket has positive probability on
one side and exactly zero on the other. Rather than silently emitting
``inf``, the default applies additive (Laplace) smoothing::

    p_smooth = (c + alpha) / (n + alpha * k)

Typical interpretation of PSI values (rule of thumb from credit-risk
practice, **not** a statistical significance test):

    PSI <  0.10   negligible shift
    0.10 - 0.25   moderate shift, worth investigating
    PSI >= 0.25   substantial shift, model/feature may need retraining

These thresholds are heuristic conventions, not proofs that the two
windows come from different distributions. Small samples inflate PSI;
always report counts alongside the metric.
"""
from __future__ import annotations

import numpy as np

# Heuristic PSI bands (convention, not statistical proof).
PSI_STABLE = 0.10
PSI_WARNING = 0.25

_EPS = 1e-12


def _as_counts(counts: np.ndarray) -> np.ndarray:
    c = np.asarray(counts, dtype=np.float64).reshape(-1)
    if c.size == 0:
        raise ValueError("empty count vector")
    if np.any(c < 0):
        raise ValueError("counts must be non-negative")
    if not np.all(np.isfinite(c)):
        raise ValueError("counts must be finite")
    return c


def smooth_probs(baseline_counts: np.ndarray, current_counts: np.ndarray,
                 alpha: float = 0.5) -> tuple[np.ndarray, np.ndarray]:
    """Turn paired count vectors into Laplace-smoothed probabilities.

    ``alpha=0`` disables smoothing (zero-probability buckets then make PSI
    infinite if the other side has mass there). ``alpha=0.5`` is the
    Jeffreys-Perks variant and is the default.
    """
    if alpha < 0:
        raise ValueError("alpha must be >= 0")
    b = _as_counts(baseline_counts)
    c = _as_counts(current_counts)
    if b.shape != c.shape:
        raise ValueError("baseline and current vectors must have the same length")
    k = b.size

    def _probs(counts: np.ndarray) -> np.ndarray:
        denom = counts.sum() + alpha * k
        if denom <= 0:
            # Empty vector with smoothing disabled: fall back to the
            # maximum-ignorance uniform distribution so downstream code
            # gets finite numbers rather than NaN; callers should still
            # treat an empty window as uninformative on their own.
            return np.full(k, 1.0 / k)
        return (counts + alpha) / denom

    p = _probs(b)
    q = _probs(c)
    return p, q


def psi(baseline_counts: np.ndarray, current_counts: np.ndarray,
        alpha: float = 0.5) -> float:
    """Population Stability Index: ``sum (q - p) * ln(q / p)``.

    Symmetric in its arguments (swapping p and q leaves the sum
    unchanged). Returns a non-negative float; with smoothing disabled and
    disjoint support the result is ``inf``.
    """
    p, q = smooth_probs(baseline_counts, current_counts, alpha=alpha)
    with np.errstate(divide="ignore", invalid="ignore"):
        terms = (q - p) * np.log(q / p)
    return float(np.sum(terms))


def js_divergence(baseline_counts: np.ndarray, current_counts: np.ndarray,
                  alpha: float = 0.0) -> float:
    """Jensen-Shannon divergence (base e), bounded in ``[0, ln 2]``.

    Symmetric and finite even without smoothing because of the mixture in
    the middle; smoothing stays available for comparability with PSI.
    """
    p, q = smooth_probs(baseline_counts, current_counts, alpha=alpha)
    m = 0.5 * (p + q)

    def _kl(a: np.ndarray, b: np.ndarray) -> float:
        mask = a > 0
        return float(np.sum(a[mask] * np.log(a[mask] / b[mask])))

    return 0.5 * _kl(p, m) + 0.5 * _kl(q, m)


def total_variation(baseline_counts: np.ndarray, current_counts: np.ndarray,
                    alpha: float = 0.0) -> float:
    """Total variation distance ``0.5 * sum |p - q|`` in ``[0, 1]``."""
    p, q = smooth_probs(baseline_counts, current_counts, alpha=alpha)
    return float(0.5 * np.sum(np.abs(p - q)))


def wasserstein_1(baseline_counts: np.ndarray, current_counts: np.ndarray,
                  edges: np.ndarray, alpha: float = 0.0) -> float:
    """1-D Wasserstein distance over fixed buckets, in feature units.

    ``edges`` is the full boundary array (length ``n_bins + 1``). The three
    special buckets (underflow, overflow, missing) participate with their
    mass parked at the outer edges / dropped for the missing bucket:

    * underflow mass is centered at ``edges[0]``
    * overflow mass is centered at ``edges[-1]``
    * the missing bucket has no numeric location, so its mass is excluded
      and the remaining probabilities are renormalized

    This is a conservative approximation (true W1 needs the raw values);
    its value is that it stays computable from stored baseline bin counts
    alone and is expressed in the feature's own units.
    """
    p, q = smooth_probs(baseline_counts, current_counts, alpha=alpha)
    e = np.asarray(edges, dtype=np.float64).reshape(-1)
    n_bins = e.size - 1
    if p.size != n_bins + 3:
        raise ValueError("count vector length must equal len(edges) + 2")
    # Bucket centers: underflow at e0, finite at midpoints, overflow at en,
    # missing bucket at the end (excluded).
    centers = np.concatenate([
        e[:1],
        0.5 * (e[:-1] + e[1:]),
        e[-1:],
    ])
    body = slice(0, n_bins + 2)  # all numeric buckets, excludes missing
    pn, qn = p[body], q[body]
    ps, qs = pn.sum(), qn.sum()
    if ps <= _EPS or qs <= _EPS:
        # One side is 100% missing: no numeric distance is definable.
        return float("nan")
    pn, qn = pn / ps, qn / qs
    cdf_diff = np.cumsum(pn) - np.cumsum(qn)
    widths = np.diff(centers)
    return float(np.sum(np.abs(cdf_diff[:-1]) * widths))


def psi_band(value: float) -> str:
    """Label a PSI value with the conventional band name."""
    if value < PSI_STABLE:
        return "stable"
    if value < PSI_WARNING:
        return "moderate"
    return "significant"
