"""Slow, independent reference implementation used solely for verification.

The hot engine (``app/engine.py``) works with integers and hand-written
``floor``/``ceil`` formulas.  This module re-derives the same swap with
:class:`fractions.Fraction` (exact rational arithmetic) so that a shared
coding mistake in the integer formulas cannot silently agree with itself.

Verification layers
-------------------
1. **Tick map** (:func:`reference_sqrt_price`): the integer fixed-point sqrt
   price recomputed from ``10**38 * 1.0001**(tick/2)`` at 120-digit Decimal
   precision.  The canonical integer mapping must equal its floor.
2. **Continuous swap** (:func:`reference_swap`): same interval segmentation
   and *same documented rounding conventions* as the engine, but every AMM
   equation is evaluated with Fractions, and the affordable-price search is
   implemented independently (different loop structure).  Realized quantities
   for an integer price step ``[sa, sb]`` are:

       curve_in = floor(exact rational dx or dy)   # never charge more
       fee      = ceil(curve_in * F / (D - F))     # never under-collect
       gross    = curve_in + fee
       output   = floor(exact rational dy or dx)   # never over-pay

3. **Economic invariants** checked in :func:`verify_against_engine`:
   segment count, liquidity, start/end prices, exact fee identity, no fee
   skipped on a non-empty segment, output <= exact floor (+1 wei quantization
   allowance), correct price direction, crossed steps land exactly on the
   boundary price, and ``spent + unspent == amount_in``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction

from .config import BASE_DEN, BASE_NUM, SQRT_SCALE
from .models import Pool, QuoteResult
from .tick import sqrt_price_at_tick


# ---------------------------------------------------------------------------
# 1. Independent tick map (Decimal exponentiation)
# ---------------------------------------------------------------------------

def reference_sqrt_price(tick: int, digits: int = 120) -> int:
    """Floor of ``SQRT_SCALE * 1.0001**(tick/2)`` via high-precision Decimal."""
    with localcontext() as ctx:
        ctx.prec = digits
        base = Decimal(BASE_NUM) / Decimal(BASE_DEN)
        value = Decimal(SQRT_SCALE) * (base ** (Decimal(tick) / 2))
        return int(value.to_integral_value(rounding="ROUND_FLOOR"))


# ---------------------------------------------------------------------------
# 2. Continuous swap reference (exact Fractions, engine rounding conventions)
# ---------------------------------------------------------------------------

@dataclass
class RefSegment:
    lower_tick: int
    upper_tick: int
    liquidity: int
    sa: int
    sb: int
    crossed: bool
    curve: int       # floor-rounded net input actually used
    fee: int         # ceil-rounded protocol fee
    gross: int
    output: int      # floor-rounded output
    exact_output: Fraction


@dataclass
class ReferenceSwap:
    segments: list[RefSegment]
    unspent: int
    stop_reason: str


def _ceil(fr: Fraction) -> int:
    return -(-fr.numerator // fr.denominator)


def _floor(fr: Fraction) -> int:
    return fr.numerator // fr.denominator


def reference_swap(pool: Pool, *, zero_for_one: bool, amount_in: int) -> ReferenceSwap:
    """Independent Fraction-based walk mirroring the engine's exact control flow.

    Index stepping is identical to the engine (explicit +-1 across shared
    boundary prices); only the per-step arithmetic differs (exact Fractions
    versus integer floor/ceil formulas).
    """
    F, D = pool.fee_numerator, pool.fee_denominator
    boundaries = pool.boundaries
    liquidity = pool.interval_liquidity
    S = SQRT_SCALE

    sp = pool.sqrt_price
    remaining = amount_in
    i = pool.interval_index_at_tick(pool.current_tick)
    segs: list[RefSegment] = []
    stop = ""

    def realized(L: int, sa: int, sb: int) -> tuple[int, int, int, Fraction]:
        """Integerized (curve, fee, output, exact_output) for step sa -> sb."""
        saF, sbF, LF = Fraction(sa), Fraction(sb), Fraction(L)
        if zero_for_one:
            c_exact = LF * S * (saF - sbF) / (saF * sbF)
            o_exact = LF * (saF - sbF) / S
        else:
            c_exact = LF * (sbF - saF) / S
            o_exact = LF * S * (sbF - saF) / (saF * sbF)
        c = _floor(c_exact)
        fee = _ceil(Fraction(c * F, D - F))
        return c, fee, _floor(o_exact), o_exact

    while True:
        if zero_for_one:
            if i < 0:
                stop = "tick_boundary"
                break
            tlo, thi = boundaries[i], boundaries[i + 1]
            slo, shi = sqrt_price_at_tick(tlo), sqrt_price_at_tick(thi)
            if i == 0 and sp <= sqrt_price_at_tick(boundaries[0]):
                stop = "tick_boundary"
                break
            if sp == slo and i > 0:
                i -= 1
                continue
            L = liquidity[i]
            if L == 0:
                stop = "empty_range"
                break
            c_bound = _ceil(Fraction(L * S * (sp - slo), sp * slo))
            g_bound = c_bound + _ceil(Fraction(c_bound * F, D - F))
            if remaining >= g_bound and sp > slo:
                o_exact = Fraction(L * (sp - slo), S)
                segs.append(RefSegment(tlo, thi, L, sp, slo, True,
                                       c_bound, g_bound - c_bound, g_bound,
                                       _floor(o_exact), o_exact))
                remaining -= g_bound
                sp = slo
                i -= 1
                continue

            def gross_down(price: int) -> int:
                c, fee, _, _ = realized(L, sp, price)
                return c + fee

            hi_p, lo_p = sp - 1, slo + 1
            if hi_p < lo_p or gross_down(hi_p) > remaining:
                stop = "input_dust"
                break
            while lo_p < hi_p:
                mid = (lo_p + hi_p) // 2
                if gross_down(mid) <= remaining:
                    hi_p = mid
                else:
                    lo_p = mid + 1
            sb = lo_p
            c, fee, o, o_exact = realized(L, sp, sb)
            if c == 0:
                stop = "input_dust"
                break
            gross = c + fee
            segs.append(RefSegment(tlo, thi, L, sp, sb, False,
                                   c, fee, gross, o, o_exact))
            remaining -= gross
            sp = sb
            stop = "input_exhausted" if remaining == 0 else "input_dust"
            break
        else:
            if i >= len(boundaries) - 1:
                stop = "tick_boundary"
                break
            tlo, thi = boundaries[i], boundaries[i + 1]
            slo, shi = sqrt_price_at_tick(tlo), sqrt_price_at_tick(thi)
            if sp >= sqrt_price_at_tick(boundaries[-1]):
                stop = "tick_boundary"
                break
            if sp == shi and i + 1 < len(boundaries) - 1:
                i += 1
                continue
            L = liquidity[i]
            if L == 0:
                stop = "empty_range"
                break
            c_bound = _ceil(Fraction(L * (shi - sp), S))
            g_bound = c_bound + _ceil(Fraction(c_bound * F, D - F))
            if remaining >= g_bound and sp < shi:
                o_exact = Fraction(L * S * (shi - sp), sp * shi)
                segs.append(RefSegment(tlo, thi, L, sp, shi, True,
                                       c_bound, g_bound - c_bound, g_bound,
                                       _floor(o_exact), o_exact))
                remaining -= g_bound
                sp = shi
                i += 1
                continue

            def gross_up(price: int) -> int:
                c, fee, _, _ = realized(L, sp, price)
                return c + fee

            lo_p, hi_p = sp + 1, shi - 1
            if hi_p < lo_p or gross_up(lo_p) > remaining:
                stop = "input_dust"
                break
            while lo_p < hi_p:
                mid = (lo_p + hi_p + 1) // 2
                if gross_up(mid) <= remaining:
                    lo_p = mid
                else:
                    hi_p = mid - 1
            sb = lo_p
            c, fee, o, o_exact = realized(L, sp, sb)
            if c == 0:
                stop = "input_dust"
                break
            gross = c + fee
            segs.append(RefSegment(tlo, thi, L, sp, sb, False,
                                   c, fee, gross, o, o_exact))
            remaining -= gross
            sp = sb
            stop = "input_exhausted" if remaining == 0 else "input_dust"
            break

    return ReferenceSwap(segments=segs, unspent=max(0, remaining), stop_reason=stop)


# ---------------------------------------------------------------------------
# 3. Verification against a real engine QuoteResult
# ---------------------------------------------------------------------------

def verify_against_engine(pool: Pool, result: QuoteResult, *, price_tol: int = 0,
                          out_tol: int = 1) -> list[str]:
    """Return human-readable discrepancies (empty list == fully verified)."""
    errors: list[str] = []
    zfo = result.direction == "zero_for_one"
    ref = reference_swap(pool, zero_for_one=zfo, amount_in=int(result.amount_in))

    if ref.stop_reason != result.stop_reason:
        pair = {ref.stop_reason, result.stop_reason}
        if pair != {"input_dust", "input_exhausted"}:
            errors.append(f"stop reason mismatch: engine={result.stop_reason} ref={ref.stop_reason}")

    traded = [s for s in result.segments if int(s.gross_input) > 0]
    if len(traded) != len(ref.segments):
        errors.append(f"traded segment count mismatch: engine={len(traded)} ref={len(ref.segments)}")

    total_out = total_gross = 0
    for n, seg in enumerate(traded):
        if n >= len(ref.segments):
            break
        rs = ref.segments[n]
        if int(seg.liquidity) != rs.liquidity:
            errors.append(f"seg{n}: liquidity mismatch {seg.liquidity} vs {rs.liquidity}")
        sa, sb = int(seg.start_sqrt_price), int(seg.end_sqrt_price)
        if abs(sa - rs.sa) > price_tol or abs(sb - rs.sb) > price_tol:
            errors.append(f"seg{n}: price step mismatch engine=({sa},{sb}) ref=({rs.sa},{rs.sb})")

        curve, fee, gross, out = (int(seg.curve_input), int(seg.fee_input),
                                  int(seg.gross_input), int(seg.output))
        if gross != curve + fee:
            errors.append(f"seg{n}: gross({gross}) != curve({curve}) + fee({fee})")
        F, D = pool.fee_numerator, pool.fee_denominator
        need_fee = _ceil(Fraction(curve * F, D - F))
        if fee != need_fee:
            errors.append(f"seg{n}: fee {fee} != ceil(curve*F/(D-F)) {need_fee}")
        if curve > 0 and fee < 1:
            errors.append(f"seg{n}: protocol fee skipped on a non-empty segment")
        if curve != rs.curve:
            errors.append(f"seg{n}: curve input {curve} != reference {rs.curve}")
        ref_out_floor = rs.exact_output.numerator // rs.exact_output.denominator
        if out > ref_out_floor + out_tol:
            errors.append(f"seg{n}: output {out} exceeds exact floor {ref_out_floor}")
        if zfo and not (sb <= sa):
            errors.append(f"seg{n}: price must fall for zero->one")
        if not zfo and not (sb >= sa):
            errors.append(f"seg{n}: price must rise for one->zero")
        if seg.crossed:
            boundary = rs.sb  # reference crossed segment ends exactly on boundary
            if sb != boundary:
                errors.append(f"seg{n}: crossed but end price {sb} != boundary {boundary}")
        total_out += out
        total_gross += gross

    if total_gross + int(result.unspent_input) != int(result.amount_in):
        errors.append(
            f"conservation broken: gross {total_gross} + unspent "
            f"{result.unspent_input} != amount_in {result.amount_in}"
        )
    if int(result.amount_out) != total_out:
        errors.append("amount_out != sum of segment outputs")
    if int(result.fee_paid) != sum(int(s.fee_input) for s in result.segments):
        errors.append("fee_paid != sum of segment fees")
    if int(result.curve_input_total) != sum(int(s.curve_input) for s in result.segments):
        errors.append("curve_input_total != sum of segment curve inputs")
    return errors
