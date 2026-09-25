"""Acceptance experiments: impulse, large-signal stress, frequency response."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .biquad import (
    FilterConfig,
    FilterResult,
    SOS,
    filter_fixed,
    filter_float,
)
from .stability import assess_stability


@dataclass
class ImpulseTest:
    n: int
    result: FilterResult
    max_abs_error: float
    rms_error: float
    tail_rms: float                 # RMS of the fixed impulse tail (limit-cycle hint)
    impulse_peak: float
    state_saturations: int
    output_saturations: int


def run_impulse_test(
    sos: SOS, n: int = 512, config: FilterConfig | None = None
) -> ImpulseTest:
    """Unit-amplitude impulse through float and fixed paths."""
    x = np.zeros(n, dtype=np.float64)
    x[0] = 1.0
    ref = filter_float(x, sos)
    res = filter_fixed(x, sos, config, reference=ref)
    err = res.y_fixed_float - res.y_reference
    tail_start = n // 2
    return ImpulseTest(
        n=n,
        result=res,
        max_abs_error=float(np.max(np.abs(err))),
        rms_error=float(np.sqrt(np.mean(err**2))),
        tail_rms=float(np.sqrt(np.mean(res.y_fixed_float[tail_start:] ** 2))),
        impulse_peak=float(np.max(np.abs(res.y_fixed_float))),
        state_saturations=res.total_state_saturations,
        output_saturations=res.output_saturations,
    )


@dataclass
class LargeSignalTest:
    amplitude: float
    n: int
    result: FilterResult
    max_abs_output: float
    input_saturations: int
    state_saturations: int
    output_saturations: int
    max_abs_error_vs_float_scaled: float
    clipped_samples: int          # fixed outputs stuck at either rail


def run_large_signal_test(
    sos: SOS,
    amplitude: float = 5.0,
    n: int = 4096,
    freq_fraction: float = 0.05,
    config: FilterConfig | None = None,
    seed: int = 1,
) -> LargeSignalTest:
    """Over-scale sinusoid + noise: stresses saturation and overflow cycles.

    The float reference receives the same (over-scale) input, so the error
    metric isolates quantization/saturation rather than input clipping.
    """
    cfg = config or FilterConfig()
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    x = amplitude * np.sin(2 * np.pi * freq_fraction * t)
    x += 0.2 * amplitude * rng.standard_normal(n)
    ref = filter_float(x, sos)
    res = filter_fixed(x, sos, cfg, reference=ref)
    err = res.y_fixed_float - res.y_reference
    rail = cfg.q_sig.max_float
    return LargeSignalTest(
        amplitude=amplitude,
        n=n,
        result=res,
        max_abs_output=float(np.max(np.abs(res.y_fixed_float))),
        input_saturations=res.input_saturations,
        state_saturations=res.total_state_saturations,
        output_saturations=res.output_saturations,
        max_abs_error_vs_float_scaled=float(np.max(np.abs(err))),
        clipped_samples=int(
            np.sum(
                (res.y_fixed_int == cfg.q_sig.qmax) | (res.y_fixed_int == cfg.q_sig.qmin)
            )
        ),
    )


def _sos_response(rows: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Complex frequency response of SOS rows at normalized frequencies ``w``."""
    z = np.exp(1j * w)
    h = np.ones_like(z, dtype=np.complex128)
    for row in rows:
        b0, b1, b2, _a0, a1, a2 = row
        h *= (b0 + b1 * z**-1 + b2 * z**-2) / (1.0 + a1 * z**-1 + a2 * z**-2)
    return h


@dataclass
class FreqBandError:
    band: str
    max_mag_db_error: float
    max_phase_deg_error: float


@dataclass
class FreqResponseTest:
    n_points: int
    max_mag_db_error: float
    max_phase_deg_error: float
    bands: list[FreqBandError] = field(default_factory=list)
    freqs: np.ndarray = None
    h_float: np.ndarray = None
    h_quant: np.ndarray = None

    def as_table(self) -> str:
        lines = ["band        max |dH| (dB)   max phase err (deg)"]
        for b in self.bands:
            lines.append(f"{b.band:<11} {b.max_mag_db_error:>13.4f} {b.max_phase_deg_error:>20.4f}")
        return "\n".join(lines)


def run_frequency_test(
    sos: SOS,
    config: FilterConfig | None = None,
    n_points: int = 1024,
    bands: dict[str, tuple[float, float]] | None = None,
    floor_db: float = -80.0,
) -> FreqResponseTest:
    """Frequency response of float design vs. *quantized* coefficient filter.

    Magnitude/phase errors are only meaningful where the response is not in
    a deep null: points with float magnitude below ``floor_db`` are excluded
    from the max-error metrics (a -200 dB null shifting to -190 dB is a
    10 dB "error" that says nothing about the implemented filter).
    """
    cfg = config or FilterConfig()
    if bands is None:
        bands = {"full": (0.0, np.pi)}
    w = np.linspace(0.0, np.pi, n_points)
    hf = _sos_response(sos.sections, w)
    hq = _sos_response(sos.quantize(cfg.q_coef, cfg.rounding).to_float_sos().sections, w)

    mag_f = 20 * np.log10(np.maximum(np.abs(hf), 1e-12))
    mag_q = 20 * np.log10(np.maximum(np.abs(hq), 1e-12))
    dmag = np.abs(mag_f - mag_q)
    # phase error unwrapped per response to avoid branch jumps
    dphase = np.abs(np.unwrap(np.angle(hf)) - np.unwrap(np.angle(hq)))
    dphase_deg = np.degrees(dphase)

    significant = mag_f >= floor_db

    def _max_or_zero(values: np.ndarray, mask: np.ndarray) -> float:
        sel = values[mask]
        return float(np.max(sel)) if sel.size else 0.0

    band_results = []
    for name, (lo, hi) in bands.items():
        msk = (w >= lo) & (w <= hi) & significant
        band_results.append(
            FreqBandError(
                band=name,
                max_mag_db_error=_max_or_zero(dmag, msk),
                max_phase_deg_error=_max_or_zero(dphase_deg, msk),
            )
        )
    return FreqResponseTest(
        n_points=n_points,
        max_mag_db_error=_max_or_zero(dmag, significant),
        max_phase_deg_error=_max_or_zero(dphase_deg, significant),
        bands=band_results,
        freqs=w / np.pi,
        h_float=hf,
        h_quant=hq,
    )


@dataclass
class AcceptanceReport:
    impulse: ImpulseTest
    large: LargeSignalTest
    freq: FreqResponseTest
    stability: object

    def summary_lines(self) -> list[str]:
        st = self.stability
        lines = [
            f"verdict                : {st.verdict}",
            f"float poles max |z|    : {st.poles_float.max_radius:.6f}",
            f"quantized poles max |z|: {st.poles_quantized.max_radius:.6f}",
            f"coefficient sat count  : {st.coefficient_saturations}",
            f"coefficient max err    : {st.max_coefficient_error:.3e}",
            "impulse:",
            f"  max |err| vs float   : {self.impulse.max_abs_error:.4e}",
            f"  RMS err              : {self.impulse.rms_error:.4e}",
            f"  tail RMS (cycles?)   : {self.impulse.tail_rms:.4e}",
            f"  state/output sat     : {self.impulse.state_saturations}/"
            f"{self.impulse.output_saturations}",
            f"large signal (A={self.large.amplitude:g}):",
            f"  input/state/out sat  : {self.large.input_saturations}/"
            f"{self.large.state_saturations}/{self.large.output_saturations}",
            f"  clipped rail samples : {self.large.clipped_samples}",
            f"  max |err| vs float   : {self.large.max_abs_error_vs_float_scaled:.4e}",
            "frequency response (quantized coeffs vs float):",
            f"  max magnitude err dB : {self.freq.max_mag_db_error:.4f}",
            f"  max phase err deg    : {self.freq.max_phase_deg_error:.4f}",
        ]
        for w_ in st.warnings:
            lines.append(f"WARNING: {w_}")
        return lines


def run_acceptance(
    sos: SOS,
    config: FilterConfig | None = None,
    impulse_n: int = 512,
    large_amplitude: float = 5.0,
    large_n: int = 4096,
    large_freq_fraction: float = 0.05,
) -> AcceptanceReport:
    """Run the full acceptance battery on one filter/config pair."""
    return AcceptanceReport(
        impulse=run_impulse_test(sos, impulse_n, config),
        large=run_large_signal_test(
            sos, large_amplitude, large_n, large_freq_fraction, config
        ),
        freq=run_frequency_test(sos, config),
        stability=assess_stability(sos, config),
    )
