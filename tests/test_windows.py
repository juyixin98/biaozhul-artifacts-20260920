"""Unit tests for window functions."""

from __future__ import annotations

import numpy as np
import pytest

from streaming_stft.windows import get_window, hann, hamming, rect, blackman

pytestmark = pytest.mark.unit


def test_periodic_hann_endpoints() -> None:
    w = hann(8)
    assert w[0] == pytest.approx(0.0, abs=1e-15)
    # Periodic window: the last sample is the penultimate point of the
    # symmetric N+1 window, hence small but non-zero (0.5 - 0.5*cos(7pi/4)).
    assert w[-1] == pytest.approx(0.1464466094067262, abs=1e-12)
    assert w[4] == pytest.approx(1.0, abs=1e-12)


def test_symmetric_hann_endpoints_zero() -> None:
    w = hann(8, periodic=False)
    assert w[0] == pytest.approx(0.0, abs=1e-15)
    assert w[-1] == pytest.approx(0.0, abs=1e-15)


def test_periodic_hamming_endpoints_nonzero() -> None:
    w = hamming(8)
    assert w[0] == pytest.approx(0.08, abs=1e-12)
    assert w[-1] == pytest.approx(0.21473088065418822, abs=1e-12)


def test_rect_window() -> None:
    assert np.array_equal(get_window("rect", 16), np.ones(16))


def test_blackman_smaller_at_edges() -> None:
    w = blackman(64)
    assert w[0] < 1e-3
    assert abs(w[32] - 1.0) < 1e-12


def test_get_window_case_insensitive_and_aliases() -> None:
    assert np.allclose(get_window("HANN", 8), hann(8))
    assert np.allclose(get_window("rectangular", 8), rect(8))


def test_get_window_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown window"):
        get_window("kaiser", 8)


def test_window_symmetry_option_differs() -> None:
    assert not np.allclose(hann(8, periodic=False), hann(8))
