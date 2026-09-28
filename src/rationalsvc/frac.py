"""Exact-number layer.

The compute kernel speaks *exact* numbers only: arbitrary-precision
:class:`~fractions.Fraction` (and, inside the fraction-free core, Python
integers).  Nothing in this module may introduce a float - ``parse_exact`` is
the single choke point through which JSON numbers become exact values.
"""
from __future__ import annotations

from fractions import Fraction
from math import lcm
from typing import Sequence

Exact = Fraction  # type alias documenting the kernel's exact scalar type


def parse_exact(token: object) -> Fraction:
    """Parse one JSON numeric token into an exact :class:`Fraction`.

    Accepted forms:

    * ``int`` (arbitrary size)
    * ``str`` holding an optional sign, digits, and *one* of:
      a terminating decimal point/exponent (``"1.5"``, ``"1e3"``) or a
      ``p/q`` ratio (``"2/3"``).
    * ``float`` is **rejected** - a float is already an approximation and the
      whole point of the service is never to silently degrade to floating
      point.  The caller raises ``INPUT_PRECISION_UNSUPPORTED``.

    Ratios are normalized by :class:`Fraction`; normalization preserves sign
    (it moves a minus to the numerator, never drops or flips it).
    """
    if isinstance(token, bool) or isinstance(token, Fraction):
        if isinstance(token, Fraction):
            return token
        raise TypeError("bool is not a matrix scalar")
    if isinstance(token, int):
        return Fraction(token)
    if isinstance(token, str):
        s = token.strip()
        if not s:
            raise ValueError("empty numeric string")
        if "/" in s:
            num, _, den = s.partition("/")
            den = den.strip()
            if den == "":
                raise ValueError(f"ratio missing denominator: {token!r}")
            return Fraction(int(num.strip()), int(den))
        # Fraction itself parses decimals and exponents exactly ("1e-3", "2.5")
        # without touching float.  Guard against inf/nan spellings.
        if s.lower() in {"inf", "-inf", "+inf", "nan", "infinity"}:
            raise ValueError(f"non-finite token: {token!r}")
        return Fraction(s)
    if isinstance(token, float):
        raise TypeError(
            "floating-point values are not accepted as exact input; send an "
            "integer, decimal string, or ratio string"
        )
    raise TypeError(f"unsupported scalar type: {type(token).__name__}")


def common_denominator(values: Sequence[Fraction]) -> int:
    """LCM of denominators - used to turn a rational row into an integer one."""
    d = 1
    for v in values:
        d = lcm(d, v.denominator)
    return d
