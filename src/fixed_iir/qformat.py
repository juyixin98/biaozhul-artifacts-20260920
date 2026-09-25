"""Signed Qm.n fixed-point primitives.

Representation: a signed integer with ``frac_bits`` fractional bits stored in a
NumPy int64 container.  Q15 (1.15) and Q31 (1.31) have one sign bit; generalized
Qm.n formats with ``m >= 1`` are also supported.

Arithmetic convention used by the biquad implementation:

* ``mul_q(a, b, q)``: Q*Q product has 2*frac fractional bits, so it is shifted
  back right by ``frac`` and rounded (then saturated by the caller).  Products
  temporarily live in int64, which is exact for two int32 multiplicands.
* ``add_q`` saturates on overflow.

Rounding modes:

* ``"truncate"``  : drop low bits, toward zero (C integer division)
* ``"floor"``     : arithmetic right shift, toward -inf (signed ``>>`` hardware)
* ``"half_up"``   : round half away from zero
* ``"convergent"``: round half to even (banker's rounding)
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

ROUND_MODES = ("truncate", "floor", "half_up", "convergent")


@dataclass(frozen=True)
class QFormat:
    """Signed Qm.n: ``int_bits`` integer bits (including sign), ``frac_bits`` fraction."""

    int_bits: int
    frac_bits: int

    def __post_init__(self) -> None:
        if self.int_bits < 1:
            raise ValueError("int_bits must be >= 1 (signed format needs a sign bit)")
        if self.frac_bits < 0:
            raise ValueError("frac_bits must be >= 0")
        if self.int_bits + self.frac_bits > 32:
            raise ValueError("total word length beyond 32 bits is not supported")

    @property
    def word_bits(self) -> int:
        return self.int_bits + self.frac_bits

    @property
    def scale(self) -> float:
        return 2.0**self.frac_bits

    @property
    def qmax(self) -> int:
        return 2 ** (self.word_bits - 1) - 1

    @property
    def qmin(self) -> int:
        return -(2 ** (self.word_bits - 1))

    @property
    def max_float(self) -> float:
        return self.qmax / self.scale

    @property
    def min_float(self) -> float:
        return self.qmin / self.scale

    def __str__(self) -> str:
        return f"Q{self.int_bits - 1}.{self.frac_bits}"


Q15 = QFormat(1, 15)
Q31 = QFormat(1, 31)


@dataclass(frozen=True)
class QuantResult:
    """Result of quantizing one float array to a Q format."""

    fixed: np.ndarray
    qformat: QFormat
    saturated_count: int
    max_abs_err: float

    @property
    def values(self) -> np.ndarray:
        """De-quantized (quantization-only) float values."""
        return to_float(self.fixed, self.qformat)


def _round_abs(ax: np.ndarray, shift: int, mode: str) -> np.ndarray:
    """Round the magnitude of int64 ``ax`` right by ``shift`` bits."""
    if shift == 0:
        return ax.copy()
    if mode == "truncate":
        return ax >> np.int64(shift)
    if mode == "half_up":
        return (ax + np.int64(1 << (shift - 1))) >> np.int64(shift)
    if mode == "convergent":
        q = ax >> np.int64(shift)
        rem = ax & np.int64((1 << shift) - 1)
        half = np.int64(1 << (shift - 1))
        bump_up = (rem > half) | ((rem == half) & (q & np.int64(1) != 0))
        return q + bump_up.astype(np.int64)
    raise ValueError(f"unknown rounding mode: {mode!r}; expected one of {ROUND_MODES}")


def shift_right_rounded(x: np.ndarray, shift: int, mode: str) -> np.ndarray:
    """Signed right shift with the requested rounding of the discarded bits."""
    x = np.asarray(x, dtype=np.int64)
    if mode not in ROUND_MODES:
        raise ValueError(f"unknown rounding mode: {mode!r}; expected one of {ROUND_MODES}")
    if mode == "floor":
        # Arithmetic shift: NumPy >> on signed ints rounds toward -infinity.
        return x >> np.int64(shift) if shift else x.copy()
    return np.sign(x).astype(np.int64) * _round_abs(np.abs(x), shift, mode)


def saturate(x: np.ndarray, q: QFormat) -> tuple[np.ndarray, int]:
    """Saturate int64 array to the Q-format range. Returns (clipped, n_saturated)."""
    x = np.asarray(x, dtype=np.int64)
    bad = (x < q.qmin) | (x > q.qmax)
    n = int(np.count_nonzero(bad))
    return np.clip(x, q.qmin, q.qmax).astype(np.int64), n


def to_fixed(
    x: np.ndarray | float,
    q: QFormat,
    rounding: str = "convergent",
) -> QuantResult:
    """Quantize float(s) with rounding then saturation into the Q format."""
    if rounding not in ROUND_MODES:
        raise ValueError(f"unknown rounding mode: {rounding!r}")
    arr = np.asarray(x, dtype=np.float64)
    if rounding == "truncate":
        n0 = np.trunc(arr * q.scale)
    elif rounding == "floor":
        n0 = np.floor(arr * q.scale)
    elif rounding == "half_up":
        n0 = np.copysign(np.floor(np.abs(arr * q.scale) + 0.5), arr * q.scale)
    else:
        n0 = np.rint(arr * q.scale)  # halves to even
    n = n0.astype(np.int64)
    sat, nsat = saturate(n, q)
    approx = sat.astype(np.float64) / q.scale
    err = float(np.max(np.abs(arr - approx))) if arr.size else 0.0
    return QuantResult(sat, q, nsat, err)


def to_float(x: np.ndarray, q: QFormat) -> np.ndarray:
    """Convert fixed-point integers back to float."""
    return np.asarray(x, dtype=np.float64) / np.float64(q.scale)


def mul_q(a: np.ndarray, b: np.ndarray, q: QFormat, rounding: str) -> np.ndarray:
    """Multiply two Q-format ints: exact int64 product, right-shift, round."""
    prod = np.asarray(a, dtype=np.int64) * np.asarray(b, dtype=np.int64)
    return shift_right_rounded(prod, q.frac_bits, rounding)


def add_q(a: np.ndarray, b: np.ndarray, q: QFormat) -> tuple[np.ndarray, int]:
    """Saturating Q-format addition."""
    return saturate(np.asarray(a, dtype=np.int64) + np.asarray(b, dtype=np.int64), q)


def wrap_int(v: int, q: QFormat) -> int:
    """Two's-complement wrap of an arbitrary int into the Q-format word.

    Models an accumulator whose overflow bits are silently dropped (common in
    hardware MACs).  Returns the wrapped value; callers detect a wrap by
    comparing against the rail.
    """
    modulus = 1 << q.word_bits
    half = 1 << (q.word_bits - 1)
    w = (int(v) + half) % modulus - half
    return w


def wrapped(v: int, q: QFormat) -> bool:
    """True if ``v`` lies outside the representable Q-format range."""
    return v < q.qmin or v > q.qmax
