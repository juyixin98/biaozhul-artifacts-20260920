"""Project-wide constants for the concentrated-liquidity quote engine.

Numeric conventions
--------------------
* Ticks are **signed integers** in the closed range ``[TICK_MIN, TICK_MAX]``.
* Price mapping::

      p(i) = BASE ** i,   BASE = 1.0001        (human-readable float price)
      s(i) = sqrt(p(i))  = BASE ** (i / 2)     (human-readable sqrt price)

* The on-chain / engine representation of a sqrt price is a project-defined
  **integer fixed-point** number with ``SQRT_SCALE = 10**38`` implied decimal
  places (38 decimals after the radix point)::

      SP(i) = floor(10**38 * 1.0001 ** (i / 2))      (see tick.py)

* All token amounts are non-negative **integers** in the token's smallest unit
  (wei).  Token *decimals* are a display concern and never enter the engine;
  the input amounts are wei.
* Liquidity ``L`` is a non-negative integer in project "liquidity units".

Rounding directions (engine-favorable, conservative for the swapper)
---------------------------------------------------------------------
All rounding in the hot path is explicit; floating point is never used.

* tick -> sqrt price: ``floor`` (canonical, matches Uniswap V3's Q64.96
  ``getSqrtRatioAtTick`` convention with base 10**38 instead of 2**96).
* amounts-out consumed in a segment: ``floor`` (never over-pay out).
* amounts-in required to reach a tick boundary: ``ceil`` (never let a swap
  cross a boundary it has not paid for).
* fees: ``ceil`` (never under-collect protocol fee; cost is never skipped).
* sqrt price after the last partial segment is nudged in the pool-favorable
  direction (down for zero->one, up for one->zero).

The fee model
-------------
A protocol fee is charged **on the net input that actually enters the curve**
of *every* executed segment (empty segments charge nothing because nothing
trades)::

    fee_in(c) = ceil(c * FEE_NUMERATOR / (FEE_DENOMINATOR - FEE_NUMERATOR))
    gross_in  = c + fee_in

so that ``c + fee`` is exactly the gross debit and ``fee / gross`` never
falls below the configured fee tier.  Divisions by zero are impossible:
``L > 0`` and ``Sa != Sb`` are preconditions of every AMM division.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Tick universe
# ---------------------------------------------------------------------------
TICK_MIN: int = -100_000
TICK_MAX: int = 100_000

# Price = BASE ** tick  (Uniswap-V3-compatible tick base)
BASE_NUM: int = 10_001
BASE_DEN: int = 10_000

# ---------------------------------------------------------------------------
# Custom fixed-point representation of sqrt prices
# ---------------------------------------------------------------------------
SQRT_DECIMALS: int = 38
SQRT_SCALE: int = 10**SQRT_DECIMALS  # one "sqrt unit" expressed as an integer

# ---------------------------------------------------------------------------
# Fee tier (30 basis points = 0.30%)
# ---------------------------------------------------------------------------
FEE_DENOMINATOR: int = 1_000_000
FEE_NUMERATOR: int = 3_000
assert 0 < FEE_NUMERATOR < FEE_DENOMINATOR

# Engine version, mixed into snapshot digests.
ENGINE_VERSION: str = "clm-0.1.0"
