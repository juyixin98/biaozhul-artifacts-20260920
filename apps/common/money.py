"""Fixed-point money helpers.

All monetary amounts in the system use Decimal with 6 fractional digits
(micro-units), max 14 integer digits. Never binary floats touch the DB.
"""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MONEY_PLACES = Decimal("0.000001")
MONEY_MAX_DIGITS = 20  # 14 integer + 6 fractional
MONEY_DECIMAL_PLACES = 6
ZERO = Decimal("0")


def quantize_money(value) -> Decimal:
    """Convert an input (str/int/Decimal) to a 6-dp non-negative Decimal."""
    if isinstance(value, float):
        # Reject floats explicitly: binary floats must never round-trip money.
        raise InvalidOperation("monetary values must be supplied as strings")
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise InvalidOperation(f"invalid decimal value: {value!r}") from exc
    if dec.is_nan() or dec.is_infinite():
        raise InvalidOperation("non-finite monetary value")
    return dec.quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)


def quantize_score(value) -> Decimal:
    """Scores are normalized 0..1 with 6 fractional digits."""
    dec = Decimal(str(value))
    return dec.quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)
