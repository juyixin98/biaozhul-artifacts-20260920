"""Independent high-precision numeric cross-check using mpmath.

The exact kernel decides the answer; mpmath only *corroborates* it by a
completely independent route (arbitrary-precision polynomial root finding).
Floating point never decides acceptance: roots are clustered at the witness
precision and globally allocated to intervals with the SAME half-open rule the
kernel uses (see :mod:`root_isolator.evidence.allocator`). Anything that cannot
be judged at the configured precision is reported ``inconclusive``.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

import mpmath

from config.settings import NumericConfig
from root_isolator.evidence.allocator import assign_half_open, cluster_roots
from root_isolator.kernel.polynomial import RationalPoly


@dataclass(frozen=True)
class NumericRootCheck:
    interval_index: int
    approximate_root: str
    multiplicity_assumed: int
    distance_to_left: str
    distance_to_right: str
    residual: str
    cluster_size: int
    status: str  # "confirmed" | "inconclusive"
    reason: str


@dataclass(frozen=True)
class MpmathReport:
    status: str  # "confirmed" | "inconclusive"
    reason: str
    distinct_real_roots: int
    interval_checks: tuple[NumericRootCheck, ...]
    unassigned_roots: tuple[str, ...]


def _to_mpf(value: Fraction, mp: Any) -> Any:
    return mp.mpf(value.numerator) / mp.mpf(value.denominator)


def _render(mp: Any, value: Any, digits: int = 25) -> str:
    return mp.nstr(value, digits)


def find_real_roots_mp(
    polynomial: RationalPoly,
    numeric: NumericConfig,
) -> tuple[list[Any], Any]:
    """Return ``(sorted real roots, mp module)`` at the configured precision.

    Complex roots whose imaginary part is not negligible are dropped.
    """

    mp = mpmath.mp
    mp.dps = max(40, (numeric.mpmath_prec * 3 + 9) // 10)
    coeffs = [_to_mpf(polynomial[k], mp) for k in range(polynomial.degree, -1, -1)]
    roots = mpmath.polyroots(coeffs, maxsteps=200, extraprec=max(40, mp.dps))
    imag_tol = mp.mpf(10) ** (-(mp.dps // 2))
    real_roots = [
        mp.re(root)
        for root in roots
        if abs(mp.im(root)) <= imag_tol * max(abs(mp.re(root)), mp.mpf(1))
    ]
    real_roots.sort()
    return real_roots, mp


def _residual(polynomial: RationalPoly, x: Any, mp: Any) -> Any:
    result = mp.mpf(0)
    for k in range(polynomial.degree, -1, -1):
        result = result * x + _to_mpf(polynomial[k], mp)
    return abs(result)


def cross_check(
    polynomial: RationalPoly,
    radical: RationalPoly,
    intervals: list[tuple[Fraction, Fraction, int]],
    numeric: NumericConfig,
) -> MpmathReport:
    """Corroborate each ``(left, right, multiplicity)`` interval globally."""

    try:
        roots, mp = find_real_roots_mp(radical, numeric)
    except Exception as exc:  # a search failure is inconclusive, never a crash
        reason = f"mpmath root search failed: {type(exc).__name__}: {exc}"
        return MpmathReport(
            status="inconclusive",
            reason=reason,
            distinct_real_roots=0,
            interval_checks=tuple(
                NumericRootCheck(i, "", m, "", "", "", 0, "inconclusive", reason)
                for i, (_, _, m) in enumerate(intervals)
            ),
            unassigned_roots=(),
        )

    cluster_tol = mp.mpf(10) ** (-(mp.dps - 10))
    endpoint_tol = mp.mpf(10) ** (-(mp.dps * 2 // 3))
    clusters = cluster_roots(roots, cluster_tol)
    centers = [cluster.center for cluster in clusters]
    left = [_to_mpf(a, mp) for a, _, _ in intervals]
    right = [_to_mpf(b, mp) for _, b, _ in intervals]
    allocation = assign_half_open(left, right, centers, endpoint_tol)

    multiplicity_by_interval = {i: mult for i, (_, _, mult) in enumerate(intervals)}
    checks: list[NumericRootCheck] = []
    problems: list[str] = []

    for i, (a, b, multiplicity) in enumerate(intervals):
        owned = allocation.clusters_per_interval[i]
        if len(owned) == 1:
            cluster = clusters[owned[0]]
            root = cluster.center
            checks.append(
                NumericRootCheck(
                    interval_index=i,
                    approximate_root=_render(mp, root),
                    multiplicity_assumed=multiplicity,
                    distance_to_left=_render(mp, root - left[i]),
                    distance_to_right=_render(mp, right[i] - root),
                    residual=_render(mp, _residual(polynomial, root, mp)),
                    cluster_size=len(cluster.members),
                    status="confirmed",
                    reason="one high-precision root allocated to this interval under the (a, b] rule",
                )
            )
        elif len(owned) == 0:
            problems.append(f"interval {i}: no high-precision root allocated")
            checks.append(
                NumericRootCheck(i, "", multiplicity, "", "", "", 0, "inconclusive",
                                 "no high-precision root allocated to this interval")
            )
        else:
            problems.append(f"interval {i}: {len(owned)} distinct high-precision roots allocated")
            checks.append(
                NumericRootCheck(i, "", multiplicity, "", "", "", 0, "inconclusive",
                                 f"{len(owned)} roots in one interval; separation failed")
            )

    unassigned = tuple(_render(mp, centers[k]) for k in allocation.unassigned)
    if unassigned:
        problems.append(f"{len(unassigned)} high-precision root(s) match no exact interval")

    if problems:
        return MpmathReport(
            status="inconclusive",
            reason="; ".join(problems),
            distinct_real_roots=len(clusters),
            interval_checks=tuple(checks),
            unassigned_roots=unassigned,
        )
    return MpmathReport(
        status="confirmed",
        reason=f"all {len(intervals)} intervals corroborated; {len(clusters)} distinct real roots",
        distinct_real_roots=len(clusters),
        interval_checks=tuple(checks),
        unassigned_roots=(),
    )
