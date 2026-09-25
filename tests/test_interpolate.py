"""Unit tests for the sub-bin interpolators."""

import numpy as np
import pytest

from spectral_peak.interpolate import (
    clamp_delta,
    hann_ratio_delta,
    jacobsen_delta,
    log_parabolic_delta,
    parabolic_delta,
    parabolic_peak,
)
from spectral_peak.windows import get_window

SR = 48000.0
N = 4096


def _spectrum_at(freq_hz: float, amp: float = 1.0, phase: float = 0.3,
                 window: str = "hann"):
    t = np.arange(N) / SR
    x = amp * np.sin(2 * np.pi * freq_hz * t + phase)
    w = get_window(window, N)
    return np.fft.rfft(x * w)


@pytest.mark.parametrize("offset", [-0.4, -0.2, 0.0, 0.13, 0.37, 0.49])
def test_hann_ratio_recovers_fractional_offset(offset):
    k0 = 100
    spec = _spectrum_at((k0 + offset) * SR / N)
    mag = np.abs(spec)
    k = int(np.argmax(mag))
    delta = hann_ratio_delta(mag[k - 1], mag[k], mag[k + 1])
    assert abs((k + delta) - (k0 + offset)) < 0.01


@pytest.mark.parametrize("offset", [-0.3, 0.0, 0.3])
def test_jacobsen_offset_sign_and_zero(offset):
    # Jacobsen underestimates |delta| for Hann windows; assert sign only.
    k0 = 100
    spec = _spectrum_at((k0 + offset) * SR / N)
    k = int(np.argmax(np.abs(spec)))
    delta = jacobsen_delta(spec[k - 1], spec[k], spec[k + 1])
    if offset == 0.0:
        assert delta == pytest.approx(0.0, abs=1e-3)
    else:
        assert np.sign(delta) == np.sign(offset)


def test_parabolic_delta_symmetry():
    # Symmetric triplet -> zero offset; right-heavy -> positive offset.
    assert parabolic_delta(1.0, 2.0, 1.0) == pytest.approx(0.0)
    assert parabolic_delta(1.0, 2.0, 1.5) > 0.0
    assert parabolic_delta(1.5, 2.0, 1.0) < 0.0


def test_parabolic_delta_flat_returns_zero():
    assert parabolic_delta(1.0, 1.0, 1.0) == 0.0


def test_log_parabolic_close_to_true_offset():
    k0, offset = 200, 0.31
    spec = _spectrum_at((k0 + offset) * SR / N)
    mag = np.abs(spec)
    k = int(np.argmax(mag))
    delta = log_parabolic_delta(mag[k - 1], mag[k], mag[k + 1])
    assert abs((k + delta) - (k0 + offset)) < 0.05


def test_parabolic_peak_at_least_center_value():
    assert parabolic_peak(1.0, 2.0, 1.4, 0.2) >= 2.0


def test_clamp_delta():
    assert clamp_delta(0.3) == (0.3, False)
    assert clamp_delta(0.9) == (0.5, True)
    assert clamp_delta(-0.9) == (-0.5, True)
