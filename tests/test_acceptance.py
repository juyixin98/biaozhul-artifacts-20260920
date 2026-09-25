"""End-to-end acceptance: stable and unstable coefficient examples.

These mirror the JSON requests in ``examples/`` and assert the acceptance
criteria: overflow accounting, limit-cycle detection, frequency-response
error, and the stable/unstable counterexamples.
"""

import numpy as np
import pytest

from fixed_iir.analysis import run_acceptance
from fixed_iir.biquad import FilterConfig, SOS
from fixed_iir.design import butter_lowpass
from fixed_iir.qformat import QFormat

LSB15 = 1.0 / 2**15


def test_accept_stable_butterworth():
    """Well-scaled 4th-order Butterworth: no overflow, tiny freq error."""
    sos = butter_lowpass(4, 0.2)
    cfg = FilterConfig(q_sig=QFormat(1, 15), q_coef=QFormat(2, 14),
                       guard_bits=2, rounding="convergent")
    rep = run_acceptance(sos, cfg, impulse_n=512, large_amplitude=0.9,
                         large_n=2048)
    assert rep.stability.verdict in ("ok", "warning")
    assert rep.stability.poles_quantized.unstable_sections == []
    # No overflow at in-range amplitudes.
    assert rep.impulse.state_saturations == 0
    assert rep.impulse.output_saturations == 0
    # Guard bits keep internal states clean even when the (noisy) input
    # occasionally exceeds full scale; the output saturates gracefully and
    # every saturation is counted.
    assert rep.large.state_saturations == 0
    assert np.max(np.abs(rep.large.result.y_fixed_float)) <= 1.0
    if rep.large.output_saturations > 0:
        assert rep.large.clipped_samples > 0
    # Quantization error bounded by a few LSBs.
    assert rep.impulse.max_abs_error < 8 * LSB15
    # Frequency response of quantized coefficients matches float design.
    assert rep.freq.max_mag_db_error < 0.05
    assert rep.freq.max_phase_deg_error < 1.0


def test_accept_large_signal_overflow_is_bounded_and_counted():
    """8x over-scale input: saturation is reported, output never escapes."""
    sos = butter_lowpass(4, 0.1)
    cfg = FilterConfig(q_sig=QFormat(1, 15), q_coef=QFormat(2, 14),
                       guard_bits=2, rounding="convergent")
    rep = run_acceptance(sos, cfg, impulse_n=256, large_amplitude=8.0,
                         large_n=2048)
    assert rep.large.input_saturations > 0
    assert np.max(np.abs(rep.large.result.y_fixed_float)) <= 1.0
    assert np.all(np.isfinite(rep.large.result.y_fixed_float))


def test_accept_unstable_counterexample_floor_rounding():
    """Float-stable (r=0.954) section floor-quantized in Q1.2 -> poles 0.5/1.5.

    Impulse response diverges to the rail; the assessment must say unsafe.
    """
    sos = SOS(np.array([[0.25, 0.0, -0.25, 1.0, -1.9, 0.91]]))
    cfg = FilterConfig(q_sig=QFormat(1, 5), q_coef=QFormat(2, 2),
                       guard_bits=1, rounding="floor")
    rep = run_acceptance(sos, cfg, impulse_n=128, large_amplitude=0.9,
                         large_n=512)
    assert rep.stability.verdict == "unsafe"
    assert rep.stability.poles_quantized.unstable_sections == [0]
    assert rep.stability.poles_quantized.max_radius == pytest.approx(1.5)
    assert rep.impulse.tail_rms > 0.5          # runaway, not decay
    assert rep.impulse.state_saturations > 0   # saturation events counted


def test_accept_wrap_overflow_limit_cycle_counterexample():
    """Stable linear coefficients + wrap arithmetic -> overflow limit cycle."""
    sos = SOS(np.array([[0.1, 0.0, 0.0, 1.0, -1.8, 0.95]]))
    cfg = FilterConfig(q_sig=QFormat(1, 15), q_coef=QFormat(2, 14),
                       guard_bits=0, rounding="convergent", overflow="wrap")
    rep = run_acceptance(sos, cfg, impulse_n=256, large_amplitude=0.9,
                         large_n=1024)
    cyc = rep.stability.limit_cycles[0]
    assert cyc.found and cyc.kind == "overflow"
    assert cyc.amplitude > 0.25
    assert cyc.period >= 2
    # The linear coefficients themselves are stable: this is purely the
    # nonlinear wrap arithmetic.
    assert rep.stability.poles_quantized.unstable_sections == []
