"""Tests for the acceptance experiment battery."""

import numpy as np
import pytest

from fixed_iir.analysis import (
    run_acceptance,
    run_frequency_test,
    run_impulse_test,
    run_large_signal_test,
)
from fixed_iir.biquad import FilterConfig, SOS
from fixed_iir.design import butter_lowpass
from fixed_iir.qformat import QFormat


def test_impulse_test_stable_filter():
    sos = butter_lowpass(4, 0.2)
    t = run_impulse_test(sos, 512)
    assert t.max_abs_error < 4 / 2**15
    assert t.tail_rms < 4 / 2**15
    assert t.state_saturations == 0
    assert t.output_saturations == 0


def test_large_signal_counts_saturations():
    sos = butter_lowpass(4, 0.1)
    t = run_large_signal_test(sos, amplitude=5.0, n=2048)
    assert t.input_saturations > 0
    assert t.max_abs_output <= 1.0  # output stays inside the signal rail
    assert np.all(np.isfinite(t.result.y_fixed_float))


def test_frequency_error_small_for_fine_coefficients():
    sos = butter_lowpass(4, 0.2)
    t = run_frequency_test(sos, FilterConfig(q_coef=QFormat(2, 14)))
    assert t.max_mag_db_error < 0.05
    assert t.max_phase_deg_error < 1.0


def test_frequency_error_grows_for_coarse_coefficients():
    sos = butter_lowpass(4, 0.2)
    fine = run_frequency_test(sos, FilterConfig(q_coef=QFormat(2, 14)))
    coarse = run_frequency_test(sos, FilterConfig(q_coef=QFormat(2, 6)))
    assert coarse.max_mag_db_error > fine.max_mag_db_error


def test_acceptance_verdict_for_unstable_counterexample():
    # float-stable coefficients that floor-quantize to poles 0.5 and 1.5
    sos = SOS(np.array([[0.25, 0.0, -0.25, 1.0, -1.9, 0.91]]))
    cfg = FilterConfig(
        q_sig=QFormat(1, 5), q_coef=QFormat(2, 2), guard_bits=1, rounding="floor"
    )
    rep = run_acceptance(sos, cfg, impulse_n=128, large_amplitude=0.9, large_n=512)
    assert rep.stability.verdict == "unsafe"
    # Impulse response runs away to the Q0.5 positive rail (1 - 2^-5).
    assert rep.impulse.impulse_peak == pytest.approx(1.0 - 1 / 2**5, abs=1e-12)
    assert rep.impulse.tail_rms > 0.5
    assert rep.impulse.state_saturations > 0


def test_acceptance_summary_lines_complete():
    sos = butter_lowpass(2, 0.3)
    rep = run_acceptance(sos, impulse_n=128, large_n=512)
    text = "\n".join(rep.summary_lines())
    for key in ("verdict", "impulse", "large signal", "frequency response"):
        assert key in text
