"""Interval abstract domain over mathematical integers.

Endpoints are arbitrary-precision Python ints.  Unbounded endpoints are
represented by ``None`` meaning -infinity (low endpoint) or +infinity
(high endpoint).  Bottom is the empty interval.

Arithmetic follows mathematical-integer semantics of the source language:
division truncates toward zero and ``%`` is the matching truncated
remainder (result takes the sign of the dividend).
"""

from __future__ import annotations

from dataclasses import dataclass


def trunc_div(a: int, b: int) -> int:
    """Mathematical integer division truncated toward zero."""
    # Python's // floors; emulate C-style truncation.
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def trunc_rem(a: int, b: int) -> int:
    """Truncated remainder: a - trunc_div(a,b)*b (sign follows a)."""
    return a - trunc_div(a, b) * b


@dataclass(frozen=True)
class Interval:
    lo: int | None    # None == -infinity
    hi: int | None    # None == +infinity
    bot: bool = False

    # ---- constructors / basic predicates -------------------------------

    @staticmethod
    def bottom() -> "Interval":
        return Interval(None, None, True)

    @staticmethod
    def top() -> "Interval":
        return Interval(None, None, False)

    @staticmethod
    def point(v: int) -> "Interval":
        return Interval(v, v, False)

    @staticmethod
    def ranged(lo: int | None, hi: int | None) -> "Interval":
        if lo is not None and hi is not None and lo > hi:
            return Interval.bottom()
        return Interval(lo, hi, False)

    @property
    def is_bottom(self) -> bool:
        return self.bot

    @property
    def is_top(self) -> bool:
        return not self.bot and self.lo is None and self.hi is None

    @property
    def is_point(self) -> bool:
        return not self.bot and self.lo == self.hi and self.lo is not None

    def __str__(self) -> str:
        if self.bot:
            return "⊥"
        l = "-∞" if self.lo is None else str(self.lo)
        h = "+∞" if self.hi is None else str(self.hi)
        return f"[{l}, {h}]"

    def to_dict(self) -> dict:
        if self.bot:
            return {"bottom": True}
        # Endpoints are arbitrary-precision ints; Python's json emits them
        # verbatim (no float coercion).
        return {"lo": self.lo, "hi": self.hi}

    def contains(self, v: int) -> bool:
        if self.bot:
            return False
        return (self.lo is None or self.lo <= v) and (
            self.hi is None or v <= self.hi
        )

    def subset_eq(self, other: "Interval") -> bool:
        """self ⊆ other"""
        if self.bot:
            return True
        if other.bot:
            return False
        return (other.lo is None or
                (self.lo is not None and other.lo <= self.lo)) and (
                other.hi is None or
                (self.hi is not None and self.hi <= other.hi))

    # ---- lattice ops ----------------------------------------------------

    def join(self, other: "Interval") -> "Interval":
        """Least interval containing both (convex hull union)."""
        if self.bot:
            return other
        if other.bot:
            return self
        lo = None
        if self.lo is not None and other.lo is not None:
            lo = min(self.lo, other.lo)
        hi = None
        if self.hi is not None and other.hi is not None:
            hi = max(self.hi, other.hi)
        return Interval(lo, hi, False)

    def meet(self, other: "Interval") -> "Interval":
        """Intersection."""
        if self.bot or other.bot:
            return Interval.bottom()
        lo = self.lo if other.lo is None else (
            other.lo if self.lo is None else max(self.lo, other.lo))
        hi = self.hi if other.hi is None else (
            other.hi if self.hi is None else min(self.hi, other.hi))
        if lo is not None and hi is not None and lo > hi:
            return Interval.bottom()
        return Interval(lo, hi, False)

    def widen(self, other: "Interval") -> "Interval":
        """Classical widening.

        ``self`` is the previous iterate and ``other`` the new one.  The
        result must contain both.  A finite bound is pushed to infinity when
        it is unstable -- either the new iterate extends beyond it, or the
        new iterate already has an infinite bound there.
        """
        if self.bot:
            return other
        if other.bot:
            return self
        lo = self.lo
        if other.lo is None or (self.lo is not None and other.lo < self.lo):
            lo = None
        hi = self.hi
        if other.hi is None or (self.hi is not None and other.hi > self.hi):
            hi = None
        return Interval(lo, hi, False)

    def narrow(self, other: "Interval") -> "Interval":
        """Classical narrowing: only bounds that are infinite in ``self``
        may be refined using ``other``.

        Requires ``other ⊆ self`` (guaranteed in the descending iteration).
        """
        if self.bot or other.bot:
            return other if self.bot else self
        lo = other.lo if self.lo is None else self.lo
        hi = other.hi if self.hi is None else self.hi
        return Interval(lo, hi, False)

    # ---- arithmetic -----------------------------------------------------

    def neg(self) -> "Interval":
        if self.bot:
            return self
        return Interval(_neg_inf(self.hi), _neg_inf(self.lo), False)

    def add(self, other: "Interval") -> "Interval":
        if self.bot or other.bot:
            return Interval.bottom()
        return Interval(_eadd(self.lo, other.lo, -1),
                        _eadd(self.hi, other.hi, +1), False)

    def sub(self, other: "Interval") -> "Interval":
        if self.bot or other.bot:
            return Interval.bottom()
        return Interval(_esub(self.lo, other.hi), _esub(self.hi, other.lo),
                        False)

    def mult(self, other: "Interval") -> "Interval":
        """Interval multiplication.

        Corner products determine the extrema.  Each endpoint is classified as
        non-negative / non-positive / zero *as an extended integer* (lo=None
        is -infinity, hi=None is +infinity), so the sign of every infinite
        corner product is known.  Products 0 * +/-infinity collapse to 0.
        """
        if self.bot or other.bot:
            return Interval.bottom()
        # (value-or-None, side) pairs; side=-1 endpoint reaches -inf there,
        # side=+1 reaches +inf there.
        xs = [(self.lo, -1), (self.hi, +1)]
        ys = [(other.lo, -1), (other.hi, +1)]
        neg_inf = pos_inf = False
        finite: list[int] = []
        for a, sa in xs:
            for b, sb in ys:
                if a is None and b is None:
                    neg_inf = pos_inf = True
                elif a is None:
                    # -infinity (sa=-1) or +infinity (sa=+1) times finite b
                    if b == 0:
                        finite.append(0)
                    else:
                        if sa * (1 if b > 0 else -1) < 0:
                            neg_inf = True
                        else:
                            pos_inf = True
                elif b is None:
                    if a == 0:
                        finite.append(0)
                    else:
                        if sb * (1 if a > 0 else -1) < 0:
                            neg_inf = True
                        else:
                            pos_inf = True
                else:
                    finite.append(a * b)
        lo = None if neg_inf else (min(finite) if finite else None)
        hi = None if pos_inf else (max(finite) if finite else None)
        return Interval(lo, hi, False)

    def div(self, other: "Interval") -> "Interval":
        """Abstract truncated division over the *non-crashing* executions.

        If the divisor interval contains 0, the caller raises a
        division-by-zero alarm separately; here we split the divisor at 0 and
        join the quotient intervals over the positive and negative divisors,
        which is the exact set of quotients for inputs where ``b != 0``.
        """
        if self.bot or other.bot:
            return Interval.bottom()
        result = Interval.bottom()
        for d in (other.meet(Interval(1, None)),
                  other.meet(Interval(None, -1))):
            if not d.is_bottom:
                result = result.join(self._div_nonzero(d))
        return result

    def _div_nonzero(self, other: "Interval") -> "Interval":
        """Division with a divisor known not to contain 0.

        Extrema of truncated division over two integer intervals are reached
        at operand endpoints and the sign points {-1, 0, 1}; we sample the
        finite candidate product set and then account for infinite operands
        by sign reasoning (an unbounded numerator or divisor can drive the
        quotient to 0 or +/-infinity).
        """
        xs = _candidate_points(self)
        ys = _candidate_points(other)
        vals = [trunc_div(x, y) for x in xs for y in ys]
        lo = min(vals) if vals else None
        hi = max(vals) if vals else None

        # Unbounded divisor magnitude drives the quotient toward 0; 0 is
        # already in [lo, hi] when the numerator straddles 0, otherwise add
        # the appropriate 0 side (it is the limit, and an integer quotient
        # can equal it for a bounded numerator with a large divisor).
        if other.lo is None or other.hi is None:
            if self.lo is not None and self.hi is not None:
                if lo is None or lo > 0:
                    lo = 0
                if hi is None or hi < 0:
                    hi = 0

        if self.lo is None:
            if other.hi is not None and other.hi < 0:
                hi = None
            elif other.lo is not None and other.lo > 0:
                lo = None
            else:
                return Interval.top()
        if self.hi is None:
            if other.hi is not None and other.hi < 0:
                lo = None
            elif other.lo is not None and other.lo > 0:
                hi = None
            else:
                return Interval.top()
        return Interval.ranged(lo, hi)

    def rem(self, other: "Interval") -> "Interval":
        """Abstract truncated remainder.

        The truncated remainder obeys ``|a % b| <= |a|`` and
        ``|a % b| < |b|`` for every nonzero divisor, so its magnitude is
        bounded by ``min(max|a|, max|b| - 1)``; the sign follows the
        dividend.  A divisor interval containing 0 is reported separately as
        an alarm by the caller, but the bound below remains valid for the
        non-crashing executions.
        """
        if self.bot or other.bot:
            return Interval.bottom()

        # Largest finite magnitude of an interval (None if it has an
        # infinite endpoint).
        def bound_mag(iv: "Interval") -> int | None:
            if iv.lo is None or iv.hi is None:
                return None
            return max(abs(iv.lo), abs(iv.hi))

        ma = bound_mag(self)
        mb_abs = bound_mag(other)
        bounds = []
        if ma is not None:
            bounds.append(ma)          # |rem| <= |a|
        if mb_abs is not None and mb_abs >= 1:
            bounds.append(mb_abs - 1)  # |rem| <= |b| - 1
        m = min(bounds) if bounds else None

        if m is None:
            if self.lo is not None and self.lo >= 0:
                return Interval(0, None)
            if self.hi is not None and self.hi <= 0:
                return Interval(None, 0)
            return Interval.top()

        nonneg = self.lo is not None and self.lo >= 0
        nonpos = self.hi is not None and self.hi <= 0
        if nonneg:
            return Interval(0, m, False)
        if nonpos:
            return Interval(-m, 0, False)
        return Interval(-m, m, False)

    # ---- condition filtering (comparisons against an interval) ---------

    def filter_less(self, bound: int) -> "Interval":
        """x with x < bound"""
        return self.meet(Interval(None, bound - 1))

    def filter_leq(self, bound: int) -> "Interval":
        """x with x <= bound"""
        return self.meet(Interval(None, bound))

    def filter_greater(self, bound: int) -> "Interval":
        """x with x > bound"""
        return self.meet(Interval(bound + 1, None))

    def filter_geq(self, bound: int) -> "Interval":
        """x with x >= bound"""
        return self.meet(Interval(bound, None))


# ---- extended-integer helpers ---------------------------------------------

def _neg_inf(v: int | None) -> int | None:
    return None if v is None else -v


def _eadd(a: int | None, b: int | None, side: int) -> int | None:
    # Interval addition has no +inf/-inf collisions at matching endpoints.
    if a is None or b is None:
        return None
    return a + b


def _esub(a: int | None, b: int | None) -> int | None:
    if a is None or b is None:
        return None
    return a - b


def _candidate_points(iv: "Interval") -> list[int]:
    """Finite points at which an interval operation can attain an extremum:
    both finite endpoints and any of {-1, 0, 1} contained in the interval.
    """
    pts = [v for v in (iv.lo, iv.hi) if v is not None]
    for v in (-1, 0, 1):
        if iv.contains(v):
            pts.append(v)
    return pts
