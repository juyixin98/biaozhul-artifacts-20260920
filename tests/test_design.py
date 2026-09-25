"""Tests for the NumPy-only filter design (analog prototype + bilinear)."""

import numpy as np
import pytest

from fixed_iir.analysis import _sos_response
from fixed_iir.design import butter_lowpass, cheby1_lowpass, dc_gain


def test_butter_minus_3db_at_edge():
    wc = np.array([np.pi * 0.2])
    for order in (1, 2, 3, 4, 6, 8):
        sos = butter_lowpass(order, 0.2)
        mag = abs(_sos_response(sos.sections, wc)[0])
        assert mag == pytest.approx(1 / np.sqrt(2), rel=2e-3)
        assert dc_gain(sos) == pytest.approx(1.0, abs=1e-10)


def test_cheby_ripple_edge_and_dc():
    rp = 1.0
    edge_gain = 10 ** (-rp / 20)
    wc = np.array([np.pi * 0.2])
    dc = np.array([0.0])
    for order in (3, 4, 5):
        sos = cheby1_lowpass(order, 0.2, rp)
        mag_edge = abs(_sos_response(sos.sections, wc)[0])
        assert mag_edge == pytest.approx(edge_gain, rel=2e-3)
        mag_dc = abs(_sos_response(sos.sections, dc)[0])
        # Odd orders: unity DC gain; even orders: -rp dB DC gain (prototype).
        expected_dc = 1.0 if order % 2 else edge_gain
        assert mag_dc == pytest.approx(expected_dc, abs=1e-9)


def test_cheby_steeper_than_butter():
    b = butter_lowpass(5, 0.2)
    c = cheby1_lowpass(5, 0.2, 1.0)
    ws = np.array([np.pi * 0.5])
    hb = 20 * np.log10(abs(_sos_response(b.sections, ws)[0]))
    hc = 20 * np.log10(abs(_sos_response(c.sections, ws)[0]))
    assert hc < hb  # more stopband attenuation


def test_design_validation():
    with pytest.raises(ValueError):
        butter_lowpass(0, 0.2)
    with pytest.raises(ValueError):
        butter_lowpass(4, 1.5)
    with pytest.raises(ValueError):
        cheby1_lowpass(4, 0.2, 0.0)
