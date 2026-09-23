"""Offline concentrated-liquidity quote engine (exact-input swaps).

Piecewise-constant liquidity math per interval with active liquidity ``L``
between adjacent initialized ticks.  Every token amount is an integer in the
token's smallest unit; every sqrt price is an integer fixed-point number with
:data:`~app.config.SQRT_SCALE` implied decimals.

AMM identities inside one interval (``S = SQRT_SCALE``)
--------------------------------------------------------
zero -> one (price falls, token0 in, token1 out)::

    dx (curve input) = L * S * (1/Sb - 1/Sa) = L*S*(Sa-Sb)/(Sa*Sb)
    dy (output)      = L * (Sa - Sb) / S

one -> zero (price rises, token1 in, token0 out)::

    dy (curve input) = L * (Sb - Sa) / S
    dx (output)      = L * S * (Sb - Sa) / (Sa*Sb)

Fees are charged on the *net curve input* of every executed segment::

    fee(c) = ceil(c * F / (D - F))          # never under-collected
    gross  = c + fee(c) = ceil(c * D / (D - F))

Nothing is ever skipped: crossing a boundary costs the exact ``ceil`` net
input required to reach it; the final partial segment is rounded pool-favorable
so the quote never delivers more output than paid for.  Empty (L == 0)
intervals stop the swap and return the full unspent input; no division is
performed when ``L == 0`` or ``Sa == Sb``.
"""

from __future__ import annotations

from .config import SQRT_SCALE as _SCALE
from .models import (
    Pool,
    QuoteResult,
    Segment,
    STOP_DUST,
    STOP_EMPTY_RANGE,
    STOP_LIMIT,
    STOP_TICK_BOUNDARY,
)
from .crypto import digest_pool, digest_quote
from .tick import tick_at_sqrt_price


def ceil_div(a: int, b: int) -> int:
    """Ceiling of a/b for non-negative a and positive b."""
    return (a + b - 1) // b


def gross_for_curve(curve_input: int, fee_num: int, fee_den: int) -> int:
    """Gross debit required to put exactly ``curve_input`` net into the curve."""
    return ceil_div(curve_input * fee_den, fee_den - fee_num)


def quote_swap(pool: Pool, *, zero_for_one: bool, amount_in: int) -> QuoteResult:
    """Quote an exact-input swap against an immutable pool snapshot.

    The pool is **not modified**.  All evidence (per-segment prices,
    liquidity, fees, outputs) is returned on the result.
    """
    if amount_in < 0:
        raise ValueError("amount_in must be non-negative")
    if amount_in == 0:
        raise ValueError("amount_in must be positive")

    F = pool.fee_numerator
    D = pool.fee_denominator
    boundaries = pool.boundaries
    liquidity = pool.interval_liquidity
    bprice = dict(zip(boundaries, pool.boundary_prices))
    edge_lo = bprice[boundaries[0]]
    edge_hi = bprice[boundaries[-1]]

    direction = "zero_for_one" if zero_for_one else "one_for_zero"
    input_token = pool.token0 if zero_for_one else pool.token1
    output_token = pool.token1 if zero_for_one else pool.token0

    sp = pool.sqrt_price
    sp0 = sp
    remaining = amount_in
    tick0 = pool.current_tick

    segments: list[Segment] = []

    stop_reason: str | None = None

    i = pool.interval_index_at_tick(tick0)

    # ------------------------------------------------------------------
    # zero -> one: price walks downward across intervals
    # ------------------------------------------------------------------
    if zero_for_one:
        while True:
            if i < 0:
                stop_reason = STOP_TICK_BOUNDARY
                break
            tlo, thi = boundaries[i], boundaries[i + 1]
            slo, shi = bprice[tlo], bprice[thi]

            # After a cross we land exactly on the upper price of the next
            # interval down (shared boundary): trade it from the top.  If we
            # have crossed below TICK_MIN there is no interval left.
            if i == 0 and sp <= edge_lo:
                stop_reason = STOP_TICK_BOUNDARY
                break
            if sp == slo and i > 0:
                i -= 1
                continue

            L = liquidity[i]
            if L == 0:
                segments.append(
                    _segment(
                        len(segments), tlo, thi, L, sp, sp,
                        input_token, 0, 0, 0, output_token, 0, False,
                        "empty liquidity interval; swap stopped",
                    )
                )
                stop_reason = STOP_EMPTY_RANGE
                break

            # Net input required to walk all the way to the lower boundary.
            c_boundary = ceil_div(L * _SCALE * (sp - slo), sp * slo)
            g_boundary = gross_for_curve(c_boundary, F, D)

            if remaining >= g_boundary and sp > slo:
                out = (L * (sp - slo)) // _SCALE
                segments.append(
                    _segment(
                        len(segments), tlo, thi, L, sp, slo,
                        input_token, g_boundary, c_boundary,
                        g_boundary - c_boundary, output_token, out, True,
                        "crossed lower tick into next interval",
                    )
                )
                remaining -= g_boundary
                sp = slo
                i -= 1  # shared edge is the next interval's upper price
                continue

            # Final partial segment inside this interval.
            sb, curve, gross, out = _partial_down(L, sp, slo, remaining, F, D)
            if curve == 0:
                segments.append(
                    _segment(
                        len(segments), tlo, thi, L, sp, sp,
                        input_token, 0, 0, 0, output_token, 0, False,
                        "remaining input below the 1-wei minimum trade",
                    )
                )
                stop_reason = STOP_DUST
                break
            segments.append(
                _segment(
                    len(segments), tlo, thi, L, sp, sb,
                    input_token, gross, curve, gross - curve,
                    output_token, out, False,
                    "final partial fill; unspent input returned",
                )
            )
            remaining -= gross
            sp = sb
            stop_reason = STOP_LIMIT if remaining == 0 else STOP_DUST
            break

    # ------------------------------------------------------------------
    # one -> zero: price walks upward across intervals
    # ------------------------------------------------------------------
    else:
        while True:
            if i >= len(boundaries) - 1:
                stop_reason = STOP_TICK_BOUNDARY
                break
            tlo, thi = boundaries[i], boundaries[i + 1]
            slo, shi = bprice[tlo], bprice[thi]

            # After a cross we land exactly on the lower price of the next
            # interval up (shared boundary): trade it from the bottom.  If we
            # have reached TICK_MAX there is no interval left.
            if sp >= edge_hi:
                stop_reason = STOP_TICK_BOUNDARY
                break
            if sp == shi and i + 1 < len(boundaries) - 1:
                i += 1
                continue

            L = liquidity[i]
            if L == 0:
                segments.append(
                    _segment(
                        len(segments), tlo, thi, L, sp, sp,
                        input_token, 0, 0, 0, output_token, 0, False,
                        "empty liquidity interval; swap stopped",
                    )
                )
                stop_reason = STOP_EMPTY_RANGE
                break

            # Net input required to reach the upper boundary.
            c_boundary = ceil_div(L * (shi - sp), _SCALE)
            g_boundary = gross_for_curve(c_boundary, F, D)

            if remaining >= g_boundary and sp < shi:
                out = (L * _SCALE * (shi - sp)) // (sp * shi)
                segments.append(
                    _segment(
                        len(segments), tlo, thi, L, sp, shi,
                        input_token, g_boundary, c_boundary,
                        g_boundary - c_boundary, output_token, out, True,
                        "crossed upper tick into next interval",
                    )
                )
                remaining -= g_boundary
                sp = shi
                i += 1  # shared edge is the next interval's lower price
                continue

            sb, curve, gross, out = _partial_up(L, sp, shi, remaining, F, D)
            if curve == 0:
                segments.append(
                    _segment(
                        len(segments), tlo, thi, L, sp, sp,
                        input_token, 0, 0, 0, output_token, 0, False,
                        "remaining input below the 1-wei minimum trade",
                    )
                )
                stop_reason = STOP_DUST
                break
            segments.append(
                _segment(
                    len(segments), tlo, thi, L, sp, sb,
                    input_token, gross, curve, gross - curve,
                    output_token, out, False,
                    "final partial fill; unspent input returned",
                )
            )
            remaining -= gross
            sp = sb
            stop_reason = STOP_LIMIT if remaining == 0 else STOP_DUST
            break

    tick_after = tick_at_sqrt_price(sp)

    amount_out = sum(int(sg.output) for sg in segments)
    fee_paid = sum(int(sg.fee_input) for sg in segments)
    curve_total = sum(int(sg.curve_input) for sg in segments)

    pool_digest = digest_pool(pool)
    result = QuoteResult(
        direction=direction,
        input_token=input_token,
        output_token=output_token,
        amount_in=str(amount_in),
        amount_out=str(amount_out),
        unspent_input=str(remaining),
        fee_paid=str(fee_paid),
        curve_input_total=str(curve_total),
        current_tick_before=tick0,
        current_tick_after=tick_after,
        sqrt_price_before=str(sp0),
        sqrt_price_after=str(sp),
        stop_reason=stop_reason or STOP_LIMIT,
        segments=tuple(segments),
        pool_digest=pool_digest,
        quote_digest="",
    )
    object.__setattr__(result, "quote_digest", digest_quote(pool_digest, result))
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _segment(
    idx: int, tlo: int, thi: int, L: int, sa: int, sb: int,
    in_token: str, gross: int, curve: int, fee: int,
    out_token: str, out: int, crossed: bool, note: str,
) -> Segment:
    return Segment(
        index=idx,
        lower_tick=tlo,
        upper_tick=thi,
        liquidity=str(L),
        start_sqrt_price=str(sa),
        end_sqrt_price=str(sb),
        input_token=in_token,
        gross_input=str(gross),
        curve_input=str(curve),
        fee_input=str(fee),
        output_token=out_token,
        output=str(out),
        crossed=crossed,
        note=note,
    )


def _partial_down(
    L: int, sa: int, slo: int, remaining: int, F: int, D: int,
) -> tuple[int, int, int, int]:
    """Final partial segment for zero->one (price ends in ``(slo, sa)``).

    Returns ``(end_sqrt_price, curve_input, gross_input, output)`` or
    ``(sa, 0, 0, 0)`` when the smallest non-free integer price step costs more
    than the remaining input.

    The gross debit ``g(sb) = floor(dx) + ceil(floor(dx) * F / (D - F))`` is a
    non-increasing function of ``sb`` (a lower end price means more input), so
    the pool-favorable *maximal affordable* end price is found by binary
    search -- no unit-by-unit scanning regardless of fixed-point magnitude.
    """
    def gross_at(sb: int) -> tuple[int, int, int]:
        c = (L * _SCALE * (sa - sb)) // (sa * sb)
        g = gross_for_curve(c, F, D)
        o = (L * (sa - sb)) // _SCALE
        return c, g, o

    hi = sa - 1
    lo = slo + 1
    if hi < lo:
        return sa, 0, 0, 0
    c_hi, g_hi, _ = gross_at(hi)
    if g_hi > remaining:
        # Even the least possible movement is unaffordable.
        return sa, 0, 0, 0

    # Leftmost (most movement, pool-favorable) affordable end price.
    while lo < hi:
        mid = (lo + hi) // 2
        _, g_mid, _ = gross_at(mid)
        if g_mid <= remaining:
            hi = mid
        else:
            lo = mid + 1
    sb = lo
    curve, gross, out = gross_at(sb)
    if curve == 0:
        # A zero-net step would skip cost and move price for free: refuse it.
        return sa, 0, 0, 0
    return sb, curve, gross, out


def _partial_up(
    L: int, sa: int, shi: int, remaining: int, F: int, D: int,
) -> tuple[int, int, int, int]:
    """Final partial segment for one->zero (price ends in ``(sa, shi)``).

    Symmetric counterpart of :func:`_partial_down`: binary-search the
    rightmost (most movement, pool-favorable) affordable end price.
    """
    def gross_at(sb: int) -> tuple[int, int, int]:
        c = (L * (sb - sa)) // _SCALE
        g = gross_for_curve(c, F, D)
        o = (L * _SCALE * (sb - sa)) // (sa * sb)
        return c, g, o

    lo = sa + 1
    hi = shi - 1
    if hi < lo:
        return sa, 0, 0, 0
    c_lo, g_lo, _ = gross_at(lo)
    if g_lo > remaining:
        return sa, 0, 0, 0

    while lo < hi:
        mid = (lo + hi + 1) // 2
        _, g_mid, _ = gross_at(mid)
        if g_mid <= remaining:
            lo = mid
        else:
            hi = mid - 1
    sb = lo
    curve, gross, out = gross_at(sb)
    if curve == 0:
        return sa, 0, 0, 0
    return sb, curve, gross, out
