"""Float64 corroboration using NumPy/SciPy (third independent witness).

Ordinary double-precision route: :func:`numpy.roots` (companion-matrix
eigenvalues), clustered because a repeated eigenvalue is typically returned as
several nearby distinct floats, then globally allocated to the exact intervals
with the same half-open rule as the kernel and the mpmath witness. A SciPy
Newton polish refines each cluster center.

Float64 is the weakest witness: it only flags gross disagreement. When double
precision cannot represent the coefficients or separate a genuinely close
pair, it reports ``inconclusive`` and never overrules the exact answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

import numpy as np
from scipy.optimize import newton

from root_isolator.evidence.allocator import Cluster, assign_half_open
from root_isolator.kernel.polynomial import RationalPoly


@dataclass(frozen=True)
class Float64Check:
    status: str  # "confirmed" | "inconclusive"
    reason: str
    distinct_real_roots_float64: int
    matched_intervals: int
    unmatched_intervals: int
    unassigned_roots: tuple[str, ...]
    approximate_roots: tuple[str, ...]


def _float64_real_roots(polynomial: RationalPoly) -> np.ndarray:
    coeffs = np.array(
        [float(polynomial[k]) for k in range(polynomial.degree, -1, -1)],
        dtype=np.float64,
    )
    if not np.all(np.isfinite(coeffs)):
        raise OverflowError("coefficients do not fit in float64")
    roots = np.roots(coeffs)
    # A repeated companion eigenvalue often splits into a conjugate pair with
    # imaginary parts up to ~sqrt(eps); accept those as real so genuine double
    # roots are not discarded. The later clustering merges the pair.
    real = roots[np.abs(roots.imag) <= 1e-6 * np.maximum(np.abs(roots.real), 1.0)]
    return np.sort(real.real)


def _polish(polynomial: RationalPoly, value: float) -> float:
    def horner(x: float) -> float:
        result = 0.0
        for k in range(polynomial.degree, -1, -1):
            result = result * x + float(polynomial[k])
        return result

    def horner_prime(x: float) -> float:
        result = 0.0
        for k in range(polynomial.degree, 0, -1):
            result = result * x + k * float(polynomial[k])
        return result

    try:
        return float(newton(horner, value, fprime=horner_prime, tol=1e-12, maxiter=50))
    except (RuntimeError, FloatingPointError):
        return float(value)


def cross_check_float64(
    polynomial: RationalPoly,
    intervals: list[tuple[Fraction, Fraction, int]],
) -> Float64Check:
    try:
        raw_roots = _float64_real_roots(polynomial)
    except (OverflowError, FloatingPointError, np.linalg.LinAlgError) as exc:
        return Float64Check(
            status="inconclusive",
            reason=f"float64 route could not represent or solve this polynomial: {exc}",
            distinct_real_roots_float64=0,
            matched_intervals=0,
            unmatched_intervals=len(intervals),
            unassigned_roots=(),
            approximate_roots=(),
        )

    # Cluster companion-eigenvalue split roots. A repeated eigenvalue of
    # multiplicity m is typically returned split by ~eps**(1/m) (sqrt(eps) for
    # a double root), so use a scale-relative threshold well above that but far
    # below genuinely separable roots. Genuinely close roots that get merged
    # stay safe: fewer clusters than intervals -> inconclusive, never a false
    # confirmation.
    def cluster_scale(value: float) -> float:
        return 1e-6 * max(1.0, abs(value))

    ordered = sorted(float(v) for v in raw_roots)
    clusters = []
    for value in ordered:
        if clusters and abs(value - clusters[-1][-1]) <= cluster_scale(value):
            clusters[-1].append(value)
        else:
            clusters.append([value])
    from root_isolator.evidence.allocator import Cluster

    cluster_objs = [
        Cluster(center=sum(group) / len(group), members=tuple(group))
        for group in clusters
    ]
    centers = [_polish(polynomial, c.center) for c in cluster_objs]
    centers.sort()

    left = [float(a) for a, _, _ in intervals]
    right = [float(b) for _, b, _ in intervals]
    endpoint_tol = 1e-7 * max(
        [1.0] + [abs(v) for v in left] + [abs(v) for v in right]
    )
    allocation = assign_half_open(left, right, centers, endpoint_tol)

    matched = sum(1 for group in allocation.clusters_per_interval if len(group) == 1)
    duplicated = [
        i for i, group in enumerate(allocation.clusters_per_interval) if len(group) > 1
    ]
    unmatched = len(intervals) - matched
    unassigned = tuple(f"{centers[k]:.12g}" for k in allocation.unassigned)

    problems: list[str] = []
    if duplicated:
        problems.append(f"{len(duplicated)} interval(s) received multiple float64 clusters")
    if unassigned:
        problems.append(f"{len(unassigned)} float64 root(s) match no exact interval")
    if unmatched:
        problems.append(f"{unmatched} interval(s) had no float64 root")

    if problems:
        status = "inconclusive"
        reason = "; ".join(problems) + " (near-degenerate or out of float64 dynamic range)"
    else:
        status = "confirmed"
        reason = (
            f"every exact isolating interval contains one float64-polished root "
            f"({len(centers)} distinct cluster(s))"
        )

    return Float64Check(
        status=status,
        reason=reason,
        distinct_real_roots_float64=len(centers),
        matched_intervals=matched,
        unmatched_intervals=unmatched,
        unassigned_roots=unassigned,
        approximate_roots=tuple(f"{c:.12g}" for c in centers[:32]),
    )
