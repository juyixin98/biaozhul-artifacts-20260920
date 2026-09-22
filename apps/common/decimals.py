"""Fixed-point decimal helpers shared by the matching engine.

All amounts and quantities are :class:`decimal.Decimal` values with at most
``ENGINE_DECIMAL_PLACES`` (8) decimal places.  Floating point is never used
for money.

Rounding rules
--------------
* Prices and quantities supplied by the user are validated to at most 8 dp.
* The quote value of every fill is ``floor(price * quantity)`` at 8 dp
  (ROUND_FLOOR).  Flooring per fill guarantees that the sum charged to a
  buyer never exceeds the quote funds frozen for that order -- rounding can
  never drive a balance negative.  The seller simply receives no sub-dust
  (< 1e-8 quote) remainder; no value is created.
* Fees are ``notional * bps / 10000`` rounded **HALF_UP** to 8 dp, charged
  in the asset each side *receives* (base for buyers, quote for sellers).
* The freeze for a limit BUY is ``HALF_UP(price * quantity)`` quote.  Since
  per-fill costs are floored and ``sum(floor(x_i)) <= floor(sum(x_i)) <=
  HALF_UP(total)``, the freeze always covers every fill; any sub-dust
  residue is released when the order completes or is canceled.
* A market BUY spends no more than its frozen quote budget: affordable
  quantity is ``floor(budget / price)``; unfilled budget is released and
  the remainder order is canceled immediately.
"""
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, getcontext

from django.conf import settings

# Plenty of headroom: Decimal(30, 8) columns.
getcontext().prec = 50

D8 = Decimal("0.00000001")
ZERO = Decimal("0")
HALF_UP = ROUND_HALF_UP
FLOOR = ROUND_FLOOR


def quantize_half_up(value) -> Decimal:
    """Round *value* to the engine scale, half away from zero."""
    return Decimal(value).quantize(D8, rounding=HALF_UP)


def quantize_floor(value) -> Decimal:
    """Round *value* **toward zero** to the engine scale."""
    return Decimal(value).quantize(D8, rounding=FLOOR)


def validate_at_most_8dp(value: Decimal, field: str) -> Decimal:
    value = Decimal(value)
    if value.as_tuple().exponent < -settings.ENGINE_DECIMAL_PLACES:
        raise ValueError(f"{field} may have at most 8 decimal places")
    return value


def fee_from_bps(notional: Decimal, bps: Decimal) -> Decimal:
    """Fee = notional * bps / 10000, rounded HALF_UP to 8 dp.

    ``bps`` is the fee rate in basis points (e.g. 10 = 0.10%).
    """
    return quantize_half_up(Decimal(notional) * Decimal(bps) / Decimal(10000))


def multiply_price_qty(price: Decimal, qty: Decimal) -> Decimal:
    """Quote cost of a fill.

    Floored at 8 dp so the sum of per-fill costs can never exceed a buyer's
    frozen quote total (which is HALF_UP rounded once, at placement).
    """
    return quantize_floor(Decimal(price) * Decimal(qty))


def limit_buy_freeze(price: Decimal, qty: Decimal) -> Decimal:
    """Quote freeze for a resting limit buy: HALF_UP single rounding."""
    return quantize_half_up(Decimal(price) * Decimal(qty))
