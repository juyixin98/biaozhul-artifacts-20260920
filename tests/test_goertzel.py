"""Unit tests for the Goertzel core."""

import numpy as np
import pytest

from dtmf_service.goertzel import goertzel_power, tone_mean_square


def _sine(freq, rate, n, amplitude=1.0):
    t = np.arange(n) / rate
    return amplitude * np.sin(2 * np.pi * freq * t)


def test_pure_tone_power_matches_theory():
    rate, n, amp = 8000, 400, 0.7
    x = _sine(1000.0, rate, n, amp)
    power = goertzel_power(x, rate, 1000.0)
    assert power == pytest.approx((amp * n / 2) ** 2, rel=1e-6)


def test_off_frequency_tone_is_attenuated():
    rate, n = 8000, 400
    x = _sine(1000.0, rate, n)
    on = goertzel_power(x, rate, 1000.0)
    off = goertzel_power(x, rate, 1200.0)
    assert off < 0.05 * on


def test_non_integer_frequency_is_exact():
    # Generalized Goertzel evaluates the DFT at the exact target frequency,
    # so it must match a direct DFT computation even when the tone does not
    # complete an integer number of cycles in the window.
    rate, n = 8000, 397
    x = _sine(941.0, rate, n, 0.5)
    power = goertzel_power(x, rate, 941.0)
    k = np.arange(n)
    direct = np.abs(np.sum(x * np.exp(-2j * np.pi * 941.0 * k / rate))) ** 2
    assert power == pytest.approx(direct, rel=1e-9)


def test_tone_mean_square_roundtrip():
    rate, n, amp = 8000, 400, 0.6
    x = _sine(770.0, rate, n, amp)
    ms = tone_mean_square(goertzel_power(x, rate, 770.0), n)
    assert ms == pytest.approx(np.mean(x**2), rel=1e-6)


def test_rejects_empty_input():
    with pytest.raises(ValueError):
        goertzel_power(np.array([]), 8000, 697.0)


def test_rejects_out_of_range_frequency():
    x = np.zeros(100)
    with pytest.raises(ValueError):
        goertzel_power(x, 8000, 0.0)
    with pytest.raises(ValueError):
        goertzel_power(x, 8000, 4000.0)
