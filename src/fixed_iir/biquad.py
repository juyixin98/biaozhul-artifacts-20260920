"""Cascaded second-order sections (SOS): float reference and fixed-point paths.

Section convention (one row)::

    [b0, b1, b2, a0, a1, a2]   with a0 == 1

difference equation::

    y[n] = b0*x[n] + b1*x[n-1] + b2*x[n-2] - a1 y[n-1] - a2 y[n-2].

Both paths use the transposed direct-form-II (DF2T) update, so the float
reference and the fixed simulation differ only in quantization::

    y  =      b0*x + s1
    s1' = s2 + b1*x - a1*y
    s2' =      b2*x - a2*y

Fixed-point scaling (all explicit, no hidden normalization):

* signal / input / output live in ``q_sig``  (default Q0.15)
* coefficients live in     ``q_coef``        (default Q1.14, range [-2, 2))
* internal states share the signal's fractional scale (one LSB = 2^-fs) and
  get ``guard_bits`` extra integer bits (default 2), i.e. the state register
  is a wider word with the same LSB; only the final cascade output is
  clipped back to ``q_sig``
* each coefficient*operand product is formed exactly (Python int, no wrap),
  shifted right by ``q_coef.frac_bits`` and rounded with the chosen mode
* every state / output accumulation applies the configured overflow handling
  (``saturate`` or two's-complement ``wrap``); overflow events are counted
  per section and in total.  The *clipped/wrapped* value is fed back,
  matching hardware behavior that can produce overflow limit cycles.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .qformat import QFormat, Q15, to_fixed, to_float, wrap_int

# Default coefficient format: one sign + one integer + 14 fraction bits.
Q_COEF_DEFAULT = QFormat(2, 14)

ROUNDING_MODES = ("truncate", "floor", "half_up", "convergent")
OVERFLOW_MODES = ("saturate", "wrap")


# ---------------------------------------------------------------------------
# Coefficient containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SOS:
    """Cascaded biquad coefficients, shape ``(n_sections, 6)``."""

    sections: np.ndarray

    def __post_init__(self) -> None:
        s = np.asarray(self.sections, dtype=np.float64)
        if s.ndim != 2 or s.shape[1] != 6:
            raise ValueError("sections must have shape (n_sections, 6)")
        if np.any(np.abs(s[:, 3] - 1.0) > 1e-9):
            raise ValueError("every a0 must equal 1.0; normalize the section first")
        if not np.all(np.isfinite(s)):
            raise ValueError("SOS coefficients must all be finite")
        # frozen dataclass with an ndarray: store a defensively copied array
        object.__setattr__(self, "sections", s.copy())

    @property
    def n_sections(self) -> int:
        return int(self.sections.shape[0])

    @property
    def b(self) -> np.ndarray:
        return self.sections[:, 0:3]

    @property
    def a(self) -> np.ndarray:
        """Feedback coefficients [a1, a2] (a0 == 1 omitted)."""
        return self.sections[:, 4:6]

    def quantize(
        self, q_coef: QFormat, rounding: str = "convergent"
    ) -> "QuantizedSOS":
        return QuantizedSOS.from_float(self, q_coef, rounding)


@dataclass(frozen=True)
class QuantizedSOS:
    """Integer-quantized coefficients in ``q_coef``."""

    b: np.ndarray  # shape (S, 3), int64 holding q_coef integers
    a: np.ndarray  # shape (S, 2), int64: quantized a1, a2
    q_coef: QFormat
    rounding: str
    saturated_count: int
    max_abs_err: float

    @classmethod
    def from_float(
        cls, sos: SOS, q_coef: QFormat, rounding: str
    ) -> "QuantizedSOS":
        qr_b = to_fixed(sos.b, q_coef, rounding)
        qr_a = to_fixed(sos.a, q_coef, rounding)
        return cls(
            b=qr_b.fixed,
            a=qr_a.fixed,
            q_coef=q_coef,
            rounding=rounding,
            saturated_count=qr_b.saturated_count + qr_a.saturated_count,
            max_abs_err=max(qr_b.max_abs_err, qr_a.max_abs_err),
        )

    @property
    def n_sections(self) -> int:
        return int(self.b.shape[0])

    def to_float_sos(self) -> SOS:
        """De-quantized sections -- the *actual* implemented filter."""
        secs = np.empty((self.n_sections, 6), dtype=np.float64)
        secs[:, 0:3] = to_float(self.b, self.q_coef)
        secs[:, 3] = 1.0
        secs[:, 4:6] = to_float(self.a, self.q_coef)
        return SOS(secs)


# ---------------------------------------------------------------------------
# Configuration / result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FilterConfig:
    """Fixed-point implementation choices."""

    q_sig: QFormat = Q15
    q_coef: QFormat = Q_COEF_DEFAULT
    guard_bits: int = 2
    rounding: str = "convergent"
    overflow: str = "saturate"

    def __post_init__(self) -> None:
        if self.guard_bits < 0:
            raise ValueError("guard_bits must be >= 0")
        # Product magnitude bound: |qmin_a|*|qmin_b| = 2^(wa-1)*2^(wb-1);
        # it stays inside signed int64 while wa + wb <= 64.
        if self.q_coef.word_bits + self.q_sig.word_bits > 64:
            raise ValueError("coefficient * signal products must fit in int64")
        if self.rounding not in ROUNDING_MODES:
            raise ValueError(f"rounding must be one of {ROUNDING_MODES}")
        if self.overflow not in OVERFLOW_MODES:
            raise ValueError(f"overflow must be one of {OVERFLOW_MODES}")

    @property
    def q_state(self) -> QFormat:
        return QFormat(self.q_sig.int_bits + self.guard_bits, self.q_sig.frac_bits)


@dataclass
class FilterResult:
    """Outputs of one fixed-point filtering run."""

    y_fixed_float: np.ndarray          # de-quantized output in q_sig units
    y_fixed_int: np.ndarray            # output integers (q_sig)
    y_reference: np.ndarray            # float64 DF2T with original coeffs
    quantized: QuantizedSOS
    config: FilterConfig
    input_saturations: int
    output_saturations: int
    state_saturations: np.ndarray      # per-section counts, shape (S,)
    final_states: list[tuple[int, int]]  # (s1, s2) per section after the run

    @property
    def total_state_saturations(self) -> int:
        return int(np.sum(self.state_saturations))


# ---------------------------------------------------------------------------
# Float reference
# ---------------------------------------------------------------------------


def filter_float(x: np.ndarray, sos: SOS) -> np.ndarray:
    """Double-precision DF2T cascade reference."""
    y = np.asarray(x, dtype=np.float64)
    for s in sos.sections:
        b0, b1, b2, _a0, a1, a2 = s
        w1 = w2 = 0.0
        out = np.empty_like(y)
        for n in range(y.shape[0]):
            xi = y[n]
            yi = b0 * xi + w1
            w1 = b1 * xi - a1 * yi + w2
            w2 = b2 * xi - a2 * yi
            out[n] = yi
        y = out
    return y


def impulse_response_float(sos: SOS, n: int) -> np.ndarray:
    x = np.zeros(n, dtype=np.float64)
    x[0] = 1.0
    return filter_float(x, sos)


# ---------------------------------------------------------------------------
# Fixed-point engine (pure-Python integer math: exact, no silent wraparound)
# ---------------------------------------------------------------------------


def _round_shift(v: int, shift: int, mode: str) -> int:
    """Right-shift integer product ``v`` by ``shift`` with the chosen rounding."""
    if shift == 0:
        return v
    if mode == "floor":
        # Python >> on int is arithmetic (toward -inf), like signed hardware.
        return v >> shift
    av = abs(v)
    if mode == "truncate":
        q = av >> shift
    elif mode == "half_up":
        q = (av + (1 << (shift - 1))) >> shift
    else:  # convergent: halves to even
        q = av >> shift
        rem = av & ((1 << shift) - 1)
        half = 1 << (shift - 1)
        if rem > half or (rem == half and (q & 1)):
            q += 1
    return q if v >= 0 else -q


def _clip(v: int, lo: int, hi: int) -> tuple[int, bool]:
    if v > hi:
        return hi, True
    if v < lo:
        return lo, True
    return v, False


def _store(v: int, q: QFormat, mode: str) -> tuple[int, bool]:
    """Apply the configured overflow handling to a raw accumulator value."""
    if mode == "wrap":
        if v < q.qmin or v > q.qmax:
            return wrap_int(v, q), True
        return v, False
    return _clip(v, q.qmin, q.qmax)


def filter_fixed(
    x: np.ndarray,
    sos: SOS,
    config: FilterConfig | None = None,
    reference: np.ndarray | None = None,
) -> FilterResult:
    """Run the fixed-point DF2T cascade with explicit rounding/saturation.

    The float reference (original, unquantized coefficients) is attached for
    comparison; pass ``reference`` to avoid recomputing it.
    """
    cfg = config or FilterConfig()
    qs = cfg.q_sig
    qst = cfg.q_state
    qc = cfg.q_coef
    fshift = qc.frac_bits  # product shift: coefficient fraction width

    qsos = sos.quantize(qc, cfg.rounding)
    S = qsos.n_sections
    b = [[int(v) for v in row] for row in qsos.b]
    a = [[int(v) for v in row] for row in qsos.a]

    # Signal and state share the SAME fractional scale (one LSB = 2^-fs);
    # guard bits only widen the state register's integer range.  The input is
    # quantized in q_sig and inter-section signals run in the wider qst word;
    # only the final output is clipped back to q_sig.  No shifts in between.
    qr_in = to_fixed(x, qs, cfg.rounding)
    in_ints = [int(v) for v in qr_in.fixed]
    in_ints, in_sat = _store_list(in_ints, qst, cfg.overflow)

    s1 = [0] * S
    s2 = [0] * S
    state_sat = [0] * S
    out_sat = 0

    current = in_ints
    for sec in range(S):
        b0, b1, b2 = b[sec]
        a1, a2 = a[sec]
        ys = []
        for xi in current:
            # y = b0*x + s1
            yv = s1[sec] + _round_shift(b0 * xi, fshift, cfg.rounding)
            yv, sat = _store(yv, qst, cfg.overflow)
            state_sat[sec] += sat
            # s1' = s2 + b1*x - a1*y
            ns1 = (
                s2[sec]
                + _round_shift(b1 * xi, fshift, cfg.rounding)
                - _round_shift(a1 * yv, fshift, cfg.rounding)
            )
            ns1, sat1 = _store(ns1, qst, cfg.overflow)
            # s2' = b2*x - a2*y
            ns2 = _round_shift(b2 * xi, fshift, cfg.rounding) - _round_shift(
                a2 * yv, fshift, cfg.rounding
            )
            ns2, sat2 = _store(ns2, qst, cfg.overflow)
            s1[sec], s2[sec] = ns1, ns2
            state_sat[sec] += sat1 + sat2
            ys.append(yv)
        current = ys

    # Final section output is in the wider qst word (same LSB as q_sig):
    # clip back to the signal format.  No shift -- guard bits only widen range.
    out_ints = []
    for v in current:
        r, sat = _store(v, qs, cfg.overflow)
        out_sat += sat
        out_ints.append(r)

    y_int = np.asarray(out_ints, dtype=np.int64)
    y_float = to_float(y_int, qs)

    ref = (
        np.asarray(reference, dtype=np.float64)
        if reference is not None
        else filter_float(x, sos)
    )

    return FilterResult(
        y_fixed_float=y_float,
        y_fixed_int=y_int,
        y_reference=ref,
        quantized=qsos,
        config=cfg,
        input_saturations=qr_in.saturated_count + in_sat,
        output_saturations=out_sat,
        state_saturations=np.asarray(state_sat, dtype=np.int64),
        final_states=[(s1[i], s2[i]) for i in range(S)],
    )


def _store_list(values: list[int], q: QFormat, mode: str) -> tuple[list[int], int]:
    n = 0
    out = []
    for v in values:
        c, overflowed = _store(v, q, mode)
        n += overflowed
        out.append(c)
    return out, n


def impulse_response_fixed(
    sos: SOS, n: int, config: FilterConfig | None = None
) -> FilterResult:
    x = np.zeros(n, dtype=np.float64)
    x[0] = 1.0
    return filter_fixed(x, sos, config)
