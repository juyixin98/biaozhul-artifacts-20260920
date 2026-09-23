"""Runnable demonstration of the quote engine without starting the HTTP server.

Usage::

    . .venv/bin/activate
    python -m examples.demo

It builds a pool with three active intervals (and an empty gap above), runs a
swap that crosses two tick boundaries in each direction, and prints the full
per-segment evidence plus the independent slow-reference verdict.
"""

from __future__ import annotations

from app.config import FEE_DENOMINATOR, FEE_NUMERATOR
from app.engine import quote_swap
from app.models import Pool, Position
from app.reference import verify_against_engine
from app.tick import format_fixed, price_at_tick_decimal


def build_pool() -> Pool:
    return Pool(
        pool_id="demo",
        token0="USDC",
        token1="WETH",
        fee_numerator=FEE_NUMERATOR,
        fee_denominator=FEE_DENOMINATOR,
        current_tick=1050,
        positions=(
            Position(900, 1000, 10**30),
            Position(1000, 1100, 2 * 10**30),
            Position(1100, 1200, 5 * 10**29),
            # no liquidity above tick 1200 -> the upward swap must stop there
        ),
    )


def show(pool: Pool, *, zero_for_one: bool, amount_in: int) -> None:
    r = quote_swap(pool, zero_for_one=zero_for_one, amount_in=amount_in)
    direction = "zero -> one (USDC in, WETH out)" if zero_for_one else "one -> zero (WETH in, USDC out)"
    print("=" * 88)
    print(f"SWAP {direction}, gross input = {amount_in}")
    print(f"  price before tick {r.current_tick_before}: {price_at_tick_decimal(r.current_tick_before)}")
    print(f"  sqrt price before (x1e38): {r.sqrt_price_before}")
    print("-" * 88)
    for s in r.segments:
        print(
            f"  seg {s.index} [{s.lower_tick:>5},{s.upper_tick:>5}) L={s.liquidity:>12}  "
            f"sqrt {format_fixed(int(s.start_sqrt_price))[:14]}..{format_fixed(int(s.end_sqrt_price))[:14]}"
        )
        print(
            f"        gross={s.gross_input:>24}  curve={s.curve_input:>24}  "
            f"fee={s.fee_input:>10}  out={s.output:>24}  crossed={s.crossed}"
        )
        if s.note:
            print(f"        note: {s.note}")
    print("-" * 88)
    print(f"  total output       : {r.amount_out}")
    print(f"  total fee          : {r.fee_paid}")
    print(f"  unspent input      : {r.unspent_input}")
    print(f"  tick after         : {r.current_tick_after}")
    print(f"  stop reason        : {r.stop_reason}")
    print(f"  pool digest        : {r.pool_digest}")
    print(f"  quote digest       : {r.quote_digest}")
    errors = verify_against_engine(pool, r)
    print(f"  Fraction/Decimal reference: {'MATCH' if not errors else 'MISMATCH ' + str(errors)}")
    assert not errors


def main() -> None:
    pool = build_pool()
    print(f"Pool snapshot {pool.pool_id!r}: {len(pool.boundaries) - 1} intervals, "
          f"fee {FEE_NUMERATOR}/{FEE_DENOMINATOR} ({FEE_NUMERATOR * 10_000 // FEE_DENOMINATOR} bps)")
    # Large enough to walk through the 1100 and 1200 boundaries; the upward
    # leg then hits the empty gap and returns the remainder.
    show(pool, zero_for_one=False, amount_in=10**40)
    show(pool, zero_for_one=True, amount_in=10**40)
    show(pool, zero_for_one=False, amount_in=10**18)   # small partial fill
    show(pool, zero_for_one=True, amount_in=1)         # dust: nothing trades


if __name__ == "__main__":
    main()
