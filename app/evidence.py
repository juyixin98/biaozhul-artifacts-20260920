"""Error evidence: independent verification of kernel-produced cells.

The kernel's answers rest on exact rational Sturm arithmetic; this module
never re-implements that logic. Instead it brings three *independent*
witnesses, none of which trusts the kernel's internal Sturm counts:

1. ``mpmath.iv`` directed-rounding interval arithmetic rigorously encloses
   ``p(lo)`` and ``p(hi)``. For an open cell both enclosures must be strictly
   one-sided (zero not contained); an exact singleton must evaluate exactly
   to zero with the interval enclosure containing zero.

2. SciPy's ``brentq`` brackets a sign-changing root of the cell's *square-free
   factor* inside the open cell. Every root of a square-free polynomial is
   simple, so this independently proves the cell actually contains a root.

3. NumPy computes roots as eigenvalues of the companion matrix — a different
   algorithm family — and its distinct real-root count is compared with the
   cell count. For high-dynamic-range coefficients float64 eigenvalues are not
   trustworthy, in which case disagreement is reported as *inconclusive* and
   never overrides the exact answer.

Every verdict is ``accepted`` / ``rejected`` / ``inconclusive`` with reasons
and the key numeric state supporting it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

import numpy as np
from mpmath import MPIntervalContext
from scipy.optimize import brentq

from . import polynomial as P
from .kernel import RootCell
from .polynomial import Poly

# Adaptive precision ceiling for directed-rounded sign probes. The probe
# starts at the configured working precision and doubles until each non-root
# endpoint has a strictly one-sided enclosure. Past this cap the evidence is
# reported inconclusive (the exact Sturm answer is still mathematically
# valid; only this independent witness could not be produced).
MAX_EVIDENCE_DPS = 4000


@dataclass(frozen=True)
class IntervalEnclosure:
    """Rigorous interval enclosure of p(x) at one rational point."""

    point: str
    lower: str
    upper: str
    contains_zero: bool
    sign: int  # -1 / +1 when strictly one-sided, else 0


@dataclass(frozen=True)
class CellEvidence:
    index: int
    verdict: str          # "accepted" | "rejected" | "inconclusive"
    reasons: tuple[str, ...]
    width: str
    width_ok: bool
    enclosure_lo: IntervalEnclosure
    enclosure_hi: IntervalEnclosure
    brentq_root: str | None
    numeric_roots: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class NumericCrossCheck:
    method: str
    dps: int
    distinct_real_roots: int
    locations: tuple[str, ...]
    reliable: bool
    note: str


@dataclass(frozen=True)
class EvidenceReport:
    accepted: bool
    inconclusive: bool
    contradicted: bool
    cells: tuple[CellEvidence, ...]
    cross_check: NumericCrossCheck
    summary: str


def _rational_interval(ctx: MPIntervalContext, x: Fraction):
    """Directed-rounded interval enclosing the exact rational x."""
    return ctx.mpf(x.numerator) / ctx.mpf(x.denominator)


def _eval_enclosure(
    poly: Poly, x: Fraction, dps: int,
) -> IntervalEnclosure:
    # A fresh per-probe interval context avoids sharing global mpmath
    # precision state across concurrent requests.
    ctx = MPIntervalContext()
    ctx.dps = dps
    xi = _rational_interval(ctx, x)
    value = ctx.mpf(poly[-1].numerator) / ctx.mpf(poly[-1].denominator)
    for c in reversed(poly[:-1]):
        term = ctx.mpf(c.numerator) / ctx.mpf(c.denominator)
        value = value * xi + term
    lo, hi = value.a, value.b
    contains_zero = ctx.mpf(0) in value
    sign = -1 if hi < 0 else (1 if lo > 0 else 0)
    return IntervalEnclosure(
        point=str(x),
        lower=_trim_str(str(lo)),
        upper=_trim_str(str(hi)),
        contains_zero=bool(contains_zero),
        sign=sign,
    )


def _adaptive_enclosures(
    poly: Poly, lo: Fraction, hi: Fraction, min_dps: int, max_dps: int,
) -> tuple[IntervalEnclosure, IntervalEnclosure, int, bool]:
    """Directed-rounded enclosures with precision doubled until strict.

    Because both the point and the coefficients are exact rationals, a
    non-root endpoint has a definite sign; raising working precision always
    eventually yields a strictly one-sided enclosure. Returns the two
    enclosures, the precision actually used, and whether strictness was
    reached within ``max_dps`` (False => caller must say undetermined).
    """
    dps = min_dps
    while True:
        enc_lo = _eval_enclosure(poly, lo, dps)
        enc_hi = _eval_enclosure(poly, hi, dps)
        strict = enc_lo.sign != 0 and enc_hi.sign != 0
        if strict or dps >= max_dps:
            return enc_lo, enc_hi, dps, strict
        dps = min(max_dps, dps * 2)


def _trim_str(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[:27] + "..." + text[-27:]


# ---------------------------------------------------------------------------
# Independent companion-eigenvalue witness (NumPy)
# ---------------------------------------------------------------------------

def companion_real_roots(poly: Poly) -> tuple[tuple[float, ...], bool]:
    """Return sorted distinct real-root approximations and a reliability flag.

    Roots are the eigenvalues of the companion matrix of the denominator-
    cleared integer polynomial. Repeated eigenvalues are clustered relatively.
    The flag is False when coefficient dynamic range or degree makes float64
    eigenvalues untrustworthy as independent evidence.
    """
    common_denom = _lcm_of_denominators(poly)
    integer_coeffs = [int(c * common_denom) for c in poly]
    asc = np.array(integer_coeffs, dtype=np.float64)
    nonzero = np.abs(asc[asc != 0])
    span = float(nonzero.max() / nonzero.min())
    reliable = span < 1e12 and len(poly) <= 60

    roots = np.roots(asc[::-1])  # np.roots expects highest power first
    real = sorted(
        float(r.real) for r in roots
        if abs(r.imag) < 1e-9 * max(1.0, abs(r.real))
    )
    distinct: list[float] = []
    for value in real:
        if not distinct or abs(value - distinct[-1]) > 1e-8 * max(1.0, abs(value)):
            distinct.append(value)
    return tuple(distinct), reliable


def _lcm_of_denominators(poly: Poly) -> int:
    result = 1
    for c in poly:
        result = result * c.denominator // _gcd(result, c.denominator)
    return result


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


# ---------------------------------------------------------------------------
# Independent sign-change witness (SciPy brentq) on the square-free factor
# ---------------------------------------------------------------------------

def brentq_in_cell(factor: Poly, cell: RootCell) -> tuple[str | None, str | None]:
    """Bracket a root of the square-free ``factor`` inside an open cell.

    Returns ``(approx_root, note)``. ``approx_root`` is None when brentq
    cannot witness a sign change (that is inconclusive, never a rejection:
    brentq is a float heuristic, the exact proof is the Sturm cell).
    """
    if cell.exact:
        return None, "exact singleton needs no float bracket"

    def f(t: float) -> float:
        total = 0.0
        # Horner with exact-Fraction coefficients cast to float.
        for c in reversed(factor):
            total = total * t + float(c)
        return total

    lo, hi = float(cell.lo), float(cell.hi)
    try:
        f_lo, f_hi = f(lo), f(hi)
    except (OverflowError, ValueError):
        return None, "float evaluation overflowed at endpoints"
    if f_lo * f_hi > 0 or f_lo == 0.0 or f_hi == 0.0:
        # Distinct float endpoints collapsed, or no sign change visible at
        # this precision — inconclusive rather than contradictory.
        return None, (
            f"no strict sign change visible in float64 "
            f"(f(lo)={f_lo:.3e}, f(hi)={f_hi:.3e})"
        )
    try:
        root = brentq(f, lo, hi, xtol=1e-14, rtol=1e-14, maxiter=200)
    except (ValueError, RuntimeError) as exc:
        return None, f"brentq failed: {type(exc).__name__}"
    inside = cell.lo < Fraction(root) < cell.hi or lo <= root <= hi
    if not inside:
        return None, f"brentq root {root!r} escaped the cell"
    return repr(root), None


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def verify_isolation(
    poly: Poly,
    radical: Poly,
    factors_by_multiplicity: dict[int, Poly],
    cells: list[RootCell],
    target_width: Fraction,
    dps: int,
    *,
    full_line: bool,
) -> EvidenceReport:
    """Assemble independent evidence.

    ``radical`` is the square-free product (distinct-root polynomial); the
    companion-eigenvalue witness runs on it because every one of its roots is
    simple, so float64 eigenvalues do not split into spurious complex pairs
    the way repeated eigenvalues do. ``full_line`` is False when the caller
    restricted the search interval, in which case the global distinct-root
    count cannot be compared (roots outside the interval are deliberately
    absent) and only per-cell witnesses apply.
    """
    cell_reports: list[CellEvidence] = []

    # The witness counts roots of the radical (simple roots), which matches
    # the number of DISTINCT real roots of the original polynomial.
    numeric_locations, numeric_reliable = companion_real_roots(radical)

    for index, cell in enumerate(cells):
        reasons: list[str] = []
        accepted = True
        inconclusive = False
        used_dps = dps

        width_ok = cell.exact or cell.width <= target_width
        if not width_ok:
            accepted = False
            reasons.append("cell width exceeds requested bound")

        factor = factors_by_multiplicity.get(cell.multiplicity)

        if cell.exact:
            enc_lo = _eval_enclosure(poly, cell.lo, dps)
            enc_hi = enc_lo
            if P.eval_exact(poly, cell.lo) != 0:
                accepted = False
                reasons.append("cell marked exact but p(point) != 0")
            if not enc_lo.contains_zero:
                accepted = False
                reasons.append("interval enclosure at exact point excludes zero")
        else:
            # Certify openness against the square-free factor: its isolated
            # root is simple, so the endpoint enclosures are strictly one
            # sided (and opposite signed). Precision escalates adaptively so
            # an arbitrarily narrow cell still gets a rigorous witness.
            probe = factor if factor is not None else poly
            enc_lo, enc_hi, used_dps, strict = _adaptive_enclosures(
                probe, cell.lo, cell.hi, dps, MAX_EVIDENCE_DPS,
            )
            if not strict:
                inconclusive = True
                reasons.append(
                    f"directed-rounded sign not strict within {MAX_EVIDENCE_DPS}"
                    f" dps — independent witness undetermined (exact Sturm "
                    f"proof unaffected)")
            else:
                if enc_lo.sign == enc_hi.sign:
                    accepted = False
                    reasons.append(
                        "square-free factor endpoint enclosures share one sign; "
                        "a simple root requires opposite signs")

        # SciPy witness against the square-free factor of this multiplicity.
        brentq_root: str | None = None
        if factor is not None and not cell.exact:
            brentq_root, note = brentq_in_cell(factor, cell)
            if brentq_root is None:
                reasons.append(f"brentq inconclusive: {note}")
            else:
                reasons.append(f"brentq independently bracketed root {brentq_root}")

        # Companion-eigenvalue proximity witness. Its absence only CONTRADICTS
        # when float64 is reliable; for an already precision-inconclusive cell
        # it is just another note.
        if cell.exact:
            point = float(cell.lo)
            tol = 1e-8 * max(1.0, abs(point))
            nearby = tuple(
                repr(v) for v in numeric_locations if abs(v - point) <= tol
            )
            if not nearby and numeric_reliable:
                reasons.append("no companion-eigenvalue root at exact point")
                accepted = False
            elif nearby:
                reasons.append(f"companion eigenvalue at point: {', '.join(nearby)}")
        else:
            pad = 10 * max(float(target_width), 1e-12)
            nearby = tuple(
                repr(v) for v in numeric_locations
                if float(cell.lo) - pad <= v <= float(cell.hi) + pad
            )
            if not nearby and numeric_reliable and not inconclusive:
                reasons.append("no companion-eigenvalue root near this cell")
                accepted = False
            elif nearby:
                reasons.append(f"companion eigenvalue nearby: {', '.join(nearby)}")
            reasons.append(f"directed-rounded evidence precision: {used_dps} dps")

        if inconclusive:
            verdict = "inconclusive"
        elif accepted:
            verdict = "accepted"
        else:
            verdict = "rejected"
        cell_reports.append(CellEvidence(
            index=index,
            verdict=verdict,
            reasons=tuple(reasons),
            width=str(cell.width),
            width_ok=width_ok,
            enclosure_lo=enc_lo,
            enclosure_hi=enc_hi,
            brentq_root=brentq_root,
            numeric_roots=nearby,
        ))

    cross_check = NumericCrossCheck(
        method="numpy companion-matrix eigenvalues (float64)",
        dps=dps,
        distinct_real_roots=len(numeric_locations),
        locations=tuple(repr(x) for x in numeric_locations),
        reliable=numeric_reliable,
        note=(
            "float64 witness within its reliability envelope"
            if numeric_reliable else
            "coefficient dynamic range/degree outside float64 reliability; "
            "witness inconclusive, exact Sturm answer stands"
        ),
    )

    n_cells = len(cell_reports)
    n_accepted = sum(c.verdict == "accepted" for c in cell_reports)
    n_inconclusive = sum(c.verdict == "inconclusive" for c in cell_reports)
    contradicted = any(c.verdict == "rejected" for c in cell_reports)
    inconclusive = any(c.verdict == "inconclusive" for c in cell_reports)

    interval_note = ("" if full_line else
                      " (global count not compared: restricted interval)")

    global_mismatch = numeric_reliable and full_line and \
        len(numeric_locations) != n_cells
    if global_mismatch:
        contradicted = True
        summary = (
            f"CONFLICT: exact kernel reports {n_cells} distinct real roots; "
            f"float64 witness reports {len(numeric_locations)}"
        )
    elif contradicted:
        summary = (
            f"REJECTED: {n_accepted}/{n_cells} cells verified, "
            f"{n_cells - n_accepted - n_inconclusive} contradicted"
            f"{interval_note}"
        )
    elif inconclusive:
        summary = (
            f"UNDETERMINED: {n_accepted}/{n_cells} cells verified, "
            f"{n_inconclusive} could not be independently certified within "
            f"{MAX_EVIDENCE_DPS} dps{interval_note}"
        )
    else:
        summary = f"{n_accepted}/{n_cells} cells independently verified{interval_note}"

    return EvidenceReport(
        accepted=not contradicted and not inconclusive,
        inconclusive=inconclusive and not contradicted,
        contradicted=contradicted,
        cells=tuple(cell_reports),
        cross_check=cross_check,
        summary=summary,
    )
