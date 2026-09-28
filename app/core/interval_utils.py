"""Low-level interval helpers built on a request's mpmath contexts."""

from __future__ import annotations

import mpmath
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from enum import Enum

from .evaluator import Evaluator


class _DecimalRound(str, Enum):
    FLOOR = "floor"
    CEIL = "ceil"

    def round(self, value: Decimal, digits: int) -> Decimal:
        # Quantum = 10 ** (exponent - digits + 1) where exponent is the
        # position of the leading significant digit.
        if value == 0:
            return Decimal(0)
        with localcontext() as ctx:
            ctx.prec = max(digits + 12, 40)
            adjusted = value.adjusted()  # floor(log10(abs(value)))
            quantum = Decimal(1).scaleb(adjusted - digits + 1)
            mode = ROUND_FLOOR if self is _DecimalRound.FLOOR else ROUND_CEILING
            return value.quantize(quantum, rounding=mode)


def _format_decimal(value: Decimal, digits: int) -> str:
    """Fixed-point rendering with exactly ``digits`` significant figures."""
    if value == 0:
        return "0." + "0" * (digits - 1)
    text = format(value, "f")
    negative = text.startswith("-")
    if negative:
        text = text[1:]
    if "." in text:
        whole, frac = text.split(".")
    else:
        whole, frac = text, ""
    sig = (whole + frac).lstrip("0")
    if len(sig) < digits:
        # pad trailing zeros to preserve the requested precision display
        if "." in format(value, "f"):
            frac = frac + "0" * (digits - len(sig))
        else:
            frac = "0" * (digits - len(sig))
            text = whole + "." + frac
            return ("-" + text) if negative else text
    if not frac:
        out = whole
    else:
        out = whole + "." + frac
    return ("-" + out) if negative else out


def _step_decimal(text: str, direction: int) -> str:
    """Move a decimal number by one unit in its last displayed place."""
    value = Decimal(text)
    _, _, exponent = value.as_tuple()
    if exponent >= 0:
        # Integer-valued text: step by 1 (text has no fractional ulp).
        step = Decimal(1)
    else:
        step = Decimal(1).scaleb(exponent)
    return format(value + direction * step, "f")


class IntervalOps:
    """Convenience operations on ivmpf enclosures for one request."""

    def __init__(self, ev: Evaluator) -> None:
        self.ev = ev
        self.iv = ev.iv
        self.mp = ev.mp
        self._zero = self.iv.mpf([0, 0])._mpi_[0]

    # -- constructors / accessors -----------------------------------------
    def make(self, lo, hi):
        return self.iv.mpf([self.mp.mpf(lo), self.mp.mpf(hi)])

    def endpoints(self, interval):
        lo_t, hi_t = interval._mpi_
        return self.mp.mpf(lo_t), self.mp.mpf(hi_t)

    def width(self, interval):
        lo, hi = self.endpoints(interval)
        return hi - lo

    def midpoint(self, interval):
        """A thin enclosure of the midpoint (rounding stays enclosed)."""
        lo, hi = self.endpoints(interval)
        m = (lo + hi) / 2
        return self.iv.mpf([m, m])

    # -- predicates ---------------------------------------------------------
    def contains_zero(self, interval) -> bool:
        lo, hi = self.endpoints(interval)
        return lo <= 0 <= hi

    def strictly_positive(self, interval) -> bool:
        lo, _ = self.endpoints(interval)
        return lo > 0

    def strictly_negative(self, interval) -> bool:
        _, hi = self.endpoints(interval)
        return hi < 0

    def is_exact_zero(self, interval) -> bool:
        """True only for a thin enclosure equal to the exact zero tuple.

        A non-trivial tiny enclosure such as [-1e-50, 1e-50] is *not* exact
        zero: it merely bounds a small nonzero value.
        """
        lo_t, hi_t = interval._mpi_
        return lo_t == self._zero and hi_t == self._zero

    def is_infinite(self, interval) -> bool:
        lo, hi = self.endpoints(interval)
        return mpmath.isinf(lo) or mpmath.isinf(hi)

    # -- set operations -----------------------------------------------------
    def intersect(self, a, b):
        """Return ``a ∩ b`` or ``None`` when disjoint."""
        a_lo, a_hi = self.endpoints(a)
        b_lo, b_hi = self.endpoints(b)
        lo = max(a_lo, b_lo)
        hi = min(a_hi, b_hi)
        if lo > hi:
            return None
        return self.make(lo, hi)

    def strictly_inside(self, inner, outer) -> bool:
        """``inner ⊂ int(outer)`` (both endpoints strict)."""
        i_lo, i_hi = self.endpoints(inner)
        o_lo, o_hi = self.endpoints(outer)
        return i_lo > o_lo and i_hi < o_hi

    def split(self, interval):
        """Bisect into two closed sub-intervals sharing the midpoint."""
        lo, hi = self.endpoints(interval)
        m = (lo + hi) / 2
        return self.make(lo, m), self.make(m, hi)

    # -- output -------------------------------------------------------------
    def outward_decimal(self, interval, digits: int) -> tuple[str, str]:
        """Outward-rounded decimal strings at ``digits`` significant figures.

        The returned decimal interval provably contains the enclosure
        (containment is verified by re-parsing at full working precision and,
        if needed, stepping outward by one decimal ulp).
        """
        lo, hi = self.endpoints(interval)
        lo_str = self._outward(lo, digits, _DecimalRound.FLOOR)
        hi_str = self._outward(hi, digits, _DecimalRound.CEIL)
        # Verify-then-adjust: guarantees the decimal interval contains [lo,hi]
        # at the request's working precision.
        while self.mp.mpf(lo_str) > lo:
            lo_str = _step_decimal(lo_str, -1)
        while self.mp.mpf(hi_str) < hi:
            hi_str = _step_decimal(hi_str, +1)
        return lo_str, hi_str

    def _outward(self, value, digits: int, direction: "_DecimalRound") -> str:
        # Serialise at the working precision (nearest), then round outward in
        # exact decimal arithmetic so binary-vs-decimal grid issues cannot
        # silently move an endpoint inward.
        exact_text = mpmath.nstr(value, self.ev.dps - 4, strip_zeros=False)
        decimal_value = Decimal(exact_text)
        rounded = direction.round(decimal_value, digits)
        return _format_decimal(rounded, digits)

    def decimal_point(self, value, digits: int) -> str:
        """A display string for a point (nearest, not a guarantee)."""
        return mpmath.nstr(self.mp.mpf(value), digits, strip_zeros=False)
