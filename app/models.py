"""Pure data model: pool snapshots, per-segment evidence, quote results.

A :class:`Pool` is an *immutable snapshot*.  Quotes are bound to a snapshot and
never mutate it; ``quote`` takes a Pool and returns a QuoteResult.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import ENGINE_VERSION, TICK_MAX, TICK_MIN
from .tick import sqrt_price_at_tick, validate_tick_range

# Stop reason vocabulary (returned with every quote).
STOP_LIMIT = "input_exhausted"        # all gross input was consumed
STOP_EMPTY_RANGE = "empty_range"      # next interval carries zero liquidity
STOP_TICK_BOUNDARY = "tick_boundary"  # reached TICK_MIN / TICK_MAX
STOP_DUST = "input_dust"              # remaining gross too small to move 1 wei


@dataclass(frozen=True)
class Position:
    """A liquidity position ``L`` concentrated on ``[lower_tick, upper_tick)``."""

    lower_tick: int
    upper_tick: int
    liquidity: int

    def validate(self) -> None:
        validate_tick_range(self.lower_tick)
        validate_tick_range(self.upper_tick)
        if self.lower_tick >= self.upper_tick:
            raise ValueError("position requires lower_tick < upper_tick")
        if self.liquidity < 0:
            raise ValueError("liquidity must be non-negative")


@dataclass(frozen=True)
class Segment:
    """Evidence for one traversed liquidity interval (one piecewise segment)."""

    index: int
    lower_tick: int
    upper_tick: int
    liquidity: str  # integer wei-like unit, serialized as string (arbitrary size)
    start_sqrt_price: str
    end_sqrt_price: str
    # Input-side token (the token spent in this segment depends on direction):
    input_token: str
    gross_input: str        # total debit (curve input + fee), wei
    curve_input: str        # input that actually entered the AMM curve, wei
    fee_input: str          # protocol fee charged on this segment, wei
    output_token: str
    output: str             # token received, floor-rounded, wei
    crossed: bool           # True iff price moved through upper/lower boundary
    note: str = ""          # "crossed", "final partial", or empty-range marker


@dataclass(frozen=True)
class QuoteResult:
    direction: str                 # "zero_for_one" | "one_for_zero"
    input_token: str
    output_token: str
    amount_in: str                 # requested gross input, wei
    amount_out: str                # total floor-rounded output, wei
    unspent_input: str             # un-filled input returned to the caller, wei
    fee_paid: str                  # total protocol fee, wei
    curve_input_total: str
    current_tick_before: int
    current_tick_after: int
    sqrt_price_before: str
    sqrt_price_after: str
    stop_reason: str
    segments: tuple[Segment, ...]
    pool_digest: str
    quote_digest: str
    engine_version: str = ENGINE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine_version": self.engine_version,
            "direction": self.direction,
            "input_token": self.input_token,
            "output_token": self.output_token,
            "amount_in": self.amount_in,
            "amount_out": self.amount_out,
            "unspent_input": self.unspent_input,
            "fee_paid": self.fee_paid,
            "curve_input_total": self.curve_input_total,
            "current_tick_before": self.current_tick_before,
            "current_tick_after": self.current_tick_after,
            "sqrt_price_before": self.sqrt_price_before,
            "sqrt_price_after": self.sqrt_price_after,
            "stop_reason": self.stop_reason,
            "segments": [s.__dict__ for s in self.segments],
            "pool_digest": self.pool_digest,
            "quote_digest": self.quote_digest,
        }


@dataclass(frozen=True)
class Pool:
    """Immutable pool snapshot with piecewise-constant concentrated liquidity."""

    token0: str
    token1: str
    fee_numerator: int
    fee_denominator: int
    current_tick: int
    positions: tuple[Position, ...] = field(default_factory=tuple)
    pool_id: str = ""

    # ----- construction ----------------------------------------------------
    def __post_init__(self) -> None:
        if not self.token0 or not self.token1 or self.token0 == self.token1:
            raise ValueError("token0 and token1 must be distinct non-empty symbols")
        validate_tick_range(self.current_tick)
        if not 0 < self.fee_numerator < self.fee_denominator:
            raise ValueError("require 0 < fee_numerator < fee_denominator")
        for p in self.positions:
            p.validate()
        # Frozen dataclass: build derived structures via object.__setattr__.
        boundaries, liquidity, boundary_prices = self._build_intervals()
        object.__setattr__(self, "_boundaries", boundaries)
        object.__setattr__(self, "_liquidity", liquidity)
        object.__setattr__(self, "_boundary_prices", boundary_prices)
        object.__setattr__(self, "_sqrt_price", sqrt_price_at_tick(self.current_tick))

    @staticmethod
    def _merge_positions(positions: tuple[Position, ...]) -> dict[tuple[int, int], int]:
        merged: dict[tuple[int, int], int] = {}
        for p in positions:
            key = (p.lower_tick, p.upper_tick)
            merged[key] = merged.get(key, 0) + p.liquidity
        return merged

    def _build_intervals(self) -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
        """Return (boundary ticks, per-interval liquidity, boundary sqrt prices).

        Boundaries always include TICK_MIN and TICK_MAX.  Interval ``k`` is
        ``[boundaries[k], boundaries[k+1])`` with active liquidity
        ``liquidity[k]``.  Fixed-point sqrt prices for every boundary are
        materialized once here so the quote hot path performs no tick map.
        """
        merged = self._merge_positions(self.positions)
        ticks: set[int] = {TICK_MIN, TICK_MAX}
        for lo, hi in merged:
            ticks.add(lo)
            ticks.add(hi)
        boundaries = tuple(sorted(ticks))

        liq: list[int] = []
        for k in range(len(boundaries) - 1):
            lo, hi = boundaries[k], boundaries[k + 1]
            total = 0
            for (plo, phi), l in merged.items():
                # position covers tick t iff plo <= t < phi; interval [lo,hi)
                # is fully inside the position iff plo <= lo and hi <= phi.
                if plo <= lo and hi <= phi:
                    total += l
            liq.append(total)
        boundary_prices = tuple(sqrt_price_at_tick(t) for t in boundaries)
        return boundaries, tuple(liq), boundary_prices

    # ----- views -----------------------------------------------------------
    @property
    def boundaries(self) -> tuple[int, ...]:
        return self._boundaries  # type: ignore[attr-defined]

    @property
    def interval_liquidity(self) -> tuple[int, ...]:
        return self._liquidity  # type: ignore[attr-defined]

    @property
    def boundary_prices(self) -> tuple[int, ...]:
        """Fixed-point sqrt price at each boundary tick (same order)."""
        return self._boundary_prices  # type: ignore[attr-defined]

    @property
    def sqrt_price(self) -> int:
        return self._sqrt_price  # type: ignore[attr-defined]

    def interval_index_at_tick(self, tick: int) -> int:
        """Index of the interval containing ``tick`` (right-open ranges)."""
        import bisect

        if tick <= TICK_MIN:
            return 0
        idx = bisect.bisect_right(self.boundaries, tick) - 1
        return min(idx, len(self.boundaries) - 2)

    def canonical(self) -> dict[str, Any]:
        """Deterministic JSON-able representation used for the digest."""
        return {
            "engine_version": ENGINE_VERSION,
            "pool_id": self.pool_id,
            "token0": self.token0,
            "token1": self.token1,
            "fee_numerator": self.fee_numerator,
            "fee_denominator": self.fee_denominator,
            "current_tick": self.current_tick,
            "positions": [
                [p.lower_tick, p.upper_tick, p.liquidity]
                for p in sorted(self.positions, key=lambda p: (p.lower_tick, p.upper_tick))
            ],
        }
