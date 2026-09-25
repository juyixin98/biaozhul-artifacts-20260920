"""Post-quantization stability and limit-cycle risk analysis.

Two complementary checks are performed:

1. **Linear stability** of the quantized coefficients.  Per section we solve
   the denominator ``z^2 + a1 z + a2 = 0`` and report the pole radii; a radius
   >= 1 means the *quantized* filter is actually unstable (the float design
   was stable, quantization pushed a pole out).  An exact Jury-style algebraic
   check (|a2| < 1, 1 + a1 + a2 > 0, 1 - a1 + a2 > 0) is evaluated too and
   agrees with the radii.

2. **Zero-input nonlinear behavior** of the implemented saturation + rounding
   arithmetic.  After exciting the filter, the input is removed and the state
   is allowed to decay; a nonzero persistent pattern is an overflow or
   quantization limit cycle, and unbounded (saturating-rail) behavior is a
   nonlinear runaway.  The search sweeps initial states / excitations because
   limit cycles depend on the state trajectory.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .biquad import (
    FilterConfig,
    QuantizedSOS,
    SOS,
)


@dataclass
class SectionPoles:
    index: int
    poles: list[complex]
    radii: list[float]
    jury_stable: bool
    jury_values: dict[str, float]


@dataclass
class PoleReport:
    sections: list[SectionPoles]

    @property
    def max_radius(self) -> float:
        return max((r for s in self.sections for r in s.radii), default=0.0)

    @property
    def unstable_sections(self) -> list[int]:
        return [s.index for s in self.sections if not s.jury_stable]

    @property
    def near_unit_sections(self) -> list[int]:
        """Sections with pole radius within 1% of the unit circle."""
        return [s.index for s in self.sections if max(s.radii) >= 0.99]


def _jury_check(a1: float, a2: float, tol: float = 0.0) -> tuple[bool, dict[str, float]]:
    """Jury stability for z^2 + a1 z + a2 (strict interior of unit disk)."""
    v = {
        "abs_a2": abs(a2),
        "one_plus_a1_plus_a2": 1.0 + a1 + a2,
        "one_minus_a1_plus_a2": 1.0 - a1 + a2,
    }
    stable = v["abs_a2"] < 1.0 - tol and v["one_plus_a1_plus_a2"] > tol and (
        v["one_minus_a1_plus_a2"] > tol
    )
    return stable, v


def _quadratic_roots(a1: float, a2: float) -> np.ndarray:
    """Roots of z^2 + a1 z + a2 = 0 (stable closed formula)."""
    if a2 == 0.0:
        return np.array([-a1, 0.0], dtype=np.complex128)
    disc = a1 * a1 - 4.0 * a2
    if disc >= 0:
        s = np.sqrt(disc)
        return np.array([(-a1 + s) / 2.0, (-a1 - s) / 2.0], dtype=np.complex128)
    s = np.sqrt(-disc)
    return np.array([(-a1 + 1j * s) / 2.0, (-a1 - 1j * s) / 2.0])


def pole_radius_report(
    sos_or_quant: SOS | QuantizedSOS, margin_tol: float = 1e-9
) -> PoleReport:
    """Report per-section pole radii of the (possibly quantized) filter."""
    if isinstance(sos_or_quant, QuantizedSOS):
        sos = sos_or_quant.to_float_sos()
    else:
        sos = sos_or_quant
    out = []
    for i, row in enumerate(sos.sections):
        a1, a2 = float(row[4]), float(row[5])
        roots = _quadratic_roots(a1, a2)
        stable, jv = _jury_check(a1, a2, margin_tol)
        out.append(
            SectionPoles(
                index=i,
                poles=[complex(r) for r in roots],
                radii=[float(abs(r)) for r in roots],
                jury_stable=stable,
                jury_values=jv,
            )
        )
    return PoleReport(out)


# ---------------------------------------------------------------------------
# Zero-input simulation on the implemented fixed-point arithmetic
# ---------------------------------------------------------------------------


@dataclass
class LimitCycleResult:
    section_index: int
    found: bool
    kind: str               # "none" | "quantization" | "overflow"
    amplitude: float        # peak |output| of the cycle (float units)
    period: int             # detected period (0 when none)
    tail: list[int]         # last samples of the zero-input run
    max_growth: float       # max |state| reached relative to state qmax


def _run_section_zero_input(
    b: tuple[int, int, int],
    a: tuple[int, int],
    s1: int,
    s2: int,
    n: int,
    cfg: FilterConfig,
) -> tuple[list[int], int, int]:
    """Run one fixed-point section with zero input from (s1, s2).

    Returns (outputs, n_overflows, max_abs_state).
    """
    from .biquad import _round_shift, _store  # reuse exact engine helpers

    qst = cfg.q_state
    a1, a2 = a
    outs: list[int] = []
    nover = 0
    max_state = 0
    for _ in range(n):
        yv, c1 = _store(s1, qst, cfg.overflow)
        ns1, c2 = _store(s2 - _round_shift(a1 * yv, cfg.q_coef.frac_bits, cfg.rounding),
                         qst, cfg.overflow)
        ns2, c3 = _store(-_round_shift(a2 * yv, cfg.q_coef.frac_bits, cfg.rounding),
                         qst, cfg.overflow)
        s1, s2 = ns1, ns2
        nover += int(c1) + int(c2) + int(c3)
        max_state = max(max_state, abs(s1), abs(s2))
        outs.append(yv)
    return outs, nover, max_state


def _detect_period(seq: list[int], max_len: int = 64) -> int:
    """Find a nonzero repeating period in the tail; 0 if none.

    Requires the last three blocks of length ``p`` to be identical so a
    transient decay is not mistaken for a cycle.
    """
    for p in range(1, min(max_len, len(seq) // 3) + 1):
        last = seq[-p:]
        if any(v != 0 for v in last) and seq[-2 * p:-p] == last and seq[-3 * p:-2 * p] == last:
            return p
    return 0


def simulate_zero_input(
    sos: SOS,
    config: FilterConfig | None = None,
    settle: int = 200,
    probe: int = 400,
    multiples: tuple[int, ...] = (1, 2),
) -> list[LimitCycleResult]:
    """Excite each quantized section, then run zero-input and detect cycles.

    A deterministic sweep of initial states (0, +/- signal full-scale
    fractions times ``multiples``) is used because limit cycles are
    state-dependent.  ``multiples=(1,)`` restricts to in-range excitation.
    """
    cfg = config or FilterConfig()
    qsos = sos.quantize(cfg.q_coef, cfg.rounding)
    qst = cfg.q_state

    # Initial states are swept at realistic *signal* full-scale levels
    # (fractions of q_sig qmax; states share the signal LSB), times the
    # given multiples.  Scaling by signal range -- not the headroom rail --
    # keeps the physical excitation fixed so that adding guard bits can
    # actually be shown to prevent overflow.
    fractions = (0.05, 0.15, 0.3, 0.5, 0.75, 0.95)
    sig_rail = cfg.q_sig.qmax
    candidates = [(0, 0)]
    for mult in multiples:
        for f in fractions:
            v = int(sig_rail * f) * mult
            candidates += [(v, 0), (-v, 0), (0, v), (0, -v), (v, v), (-v, -v),
                           (v, -v), (-v, v)]

    results = []
    rail = qst.qmax
    for sec in range(qsos.n_sections):
        b = tuple(int(v) for v in qsos.b[sec])
        a = tuple(int(v) for v in qsos.a[sec])
        best: LimitCycleResult | None = None
        for s1_0, s2_0 in candidates:
            outs, nsat, max_state = _run_section_zero_input(
                b, a, s1_0, s2_0, settle + probe, cfg
            )
            tail = outs[settle:]
            tail_tail = outs[-probe:]
            peak = max((abs(v) for v in tail_tail), default=0)
            period = _detect_period(outs)
            nonzero_tail = peak > 0
            # Persistent saturation on a stable-by-design section => overflow
            # runaway / overflow limit cycle.
            rail_hits = sum(1 for v in tail_tail if abs(v) >= rail - 1)
            if nonzero_tail and (period > 0 or rail_hits > probe // 4):
                kind = "overflow" if (nsat > 0 and peak >= rail * 0.5) else "quantization"
                amp = peak / (2 ** cfg.q_sig.frac_bits)
                cand = LimitCycleResult(
                    section_index=sec,
                    found=True,
                    kind=kind,
                    amplitude=amp,
                    period=period,
                    tail=[int(v) for v in tail_tail[-16:]],
                    max_growth=max_state / rail,
                )
                if best is None or cand.amplitude > best.amplitude:
                    best = cand
        if best is None:
            best = LimitCycleResult(
                section_index=sec,
                found=False,
                kind="none",
                amplitude=0.0,
                period=0,
                tail=[],
                max_growth=0.0,
            )
        results.append(best)
    return results


# ---------------------------------------------------------------------------
# Combined assessment
# ---------------------------------------------------------------------------


@dataclass
class StabilityReport:
    stable_float: bool
    quantized: QuantizedSOS
    poles_float: PoleReport
    poles_quantized: PoleReport
    coefficient_saturations: int
    max_coefficient_error: float
    limit_cycles: list[LimitCycleResult]
    verdict: str
    warnings: list[str] = field(default_factory=list)


def assess_stability(
    sos: SOS,
    config: FilterConfig | None = None,
    near_margin: float = 0.02,
) -> StabilityReport:
    """Full quantization-risk assessment; verdict in {ok, warning, unsafe}."""
    cfg = config or FilterConfig()
    poles_f = pole_radius_report(sos)
    qsos = sos.quantize(cfg.q_coef, cfg.rounding)
    poles_q = pole_radius_report(qsos)

    warnings: list[str] = []
    if poles_f.unstable_sections:
        warnings.append(
            f"float reference itself is unstable in sections {poles_f.unstable_sections}"
        )
    if poles_q.unstable_sections:
        warnings.append(
            "quantized poles outside unit circle in sections "
            f"{poles_q.unstable_sections} (max radius {poles_q.max_radius:.6f})"
        )
    near = [s.index for s in poles_q.sections if max(s.radii) >= 1.0 - near_margin]
    if near and not poles_q.unstable_sections:
        warnings.append(
            f"quantized poles within {near_margin:.0%} of unit circle in sections {near}"
        )
    if qsos.saturated_count:
        warnings.append(
            f"{qsos.saturated_count} coefficient(s) saturated into {cfg.q_coef}"
        )

    cycles = simulate_zero_input(sos, cfg)
    for c in cycles:
        if c.found:
            warnings.append(
                f"section {c.section_index}: {c.kind} limit cycle, "
                f"amplitude ~{c.amplitude:.4g}, period {c.period}"
            )

    unsafe = bool(poles_q.unstable_sections) or any(
        c.found and c.kind == "overflow" for c in cycles
    )
    warn = bool(warnings)
    verdict = "unsafe" if unsafe else ("warning" if warn else "ok")
    return StabilityReport(
        stable_float=not poles_f.unstable_sections,
        quantized=qsos,
        poles_float=poles_f,
        poles_quantized=poles_q,
        coefficient_saturations=qsos.saturated_count,
        max_coefficient_error=qsos.max_abs_err,
        limit_cycles=cycles,
        verdict=verdict,
        warnings=warnings,
    )
