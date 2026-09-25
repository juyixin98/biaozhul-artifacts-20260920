"""Tests for float and fixed-point cascaded biquad filtering."""

import numpy as np
import pytest

from fixed_iir.biquad import (
    FilterConfig,
    SOS,
    filter_fixed,
    filter_float,
    impulse_response_fixed,
)
from fixed_iir.design import butter_lowpass, cheby1_lowpass, dc_gain
from fixed_iir.qformat import Q15, Q31, QFormat


# A known second-order section: y[n] = 0.5 x[n] + 0.5 x[n-1] (2-tap FIR padded)
FIR_ROW = np.array([[0.5, 0.5, 0.0, 1.0, 0.0, 0.0]])


def test_float_fir_row_matches_direct():
    sos = SOS(FIR_ROW)
    x = np.array([1.0, 2.0, 3.0, 4.0])
    y = filter_float(x, sos)
    np.testing.assert_allclose(y, [0.5, 1.5, 2.5, 3.5])


def test_float_first_order_recursion():
    # y[n] = x[n] + 0.5 y[n-1]
    sos = SOS(np.array([[1.0, 0.0, 0.0, 1.0, -0.5, 0.0]]))
    y = filter_float(np.array([1.0, 0.0, 0.0, 0.0]), sos)
    np.testing.assert_allclose(y, [1.0, 0.5, 0.25, 0.125])


def test_sos_validation():
    with pytest.raises(ValueError):
        SOS(np.zeros((2, 5)))
    with pytest.raises(ValueError):
        SOS(np.array([[1, 0, 0, 2, 0, 0]], dtype=float))  # a0 != 1
    with pytest.raises(ValueError):
        SOS(np.array([[1, np.nan, 0, 1, 0, 0]]))


def test_sos_is_defensively_copied():
    rows = FIR_ROW.copy()
    sos = SOS(rows)
    rows[0, 0] = 99.0
    assert sos.sections[0, 0] == 0.5


def test_butter_dc_gain_unity():
    for order in (1, 2, 3, 4, 6, 8):
        sos = butter_lowpass(order, 0.25)
        assert abs(dc_gain(sos) - 1.0) < 1e-10
    assert abs(dc_gain(cheby1_lowpass(5, 0.2, 0.5)) - 1.0) < 1e-10


def test_butter_poles_inside_unit_circle():
    from fixed_iir.stability import pole_radius_report
    sos = butter_lowpass(8, 0.1)
    rep = pole_radius_report(sos)
    assert rep.max_radius < 1.0
    assert rep.unstable_sections == []


def test_fixed_matches_fir_exactly():
    sos = SOS(FIR_ROW)
    x = np.array([0.5, -0.25, 0.125, 1.0])
    res = filter_fixed(x, sos)
    np.testing.assert_allclose(res.y_fixed_float,
                               filter_float(x, sos), atol=1e-9)
    assert res.output_saturations == 0


def test_fixed_impulse_close_to_float_butter():
    sos = butter_lowpass(4, 0.2)
    res = impulse_response_fixed(sos, 512)
    err = np.abs(res.y_fixed_float - res.y_reference)
    lsb = 1.0 / 2**15
    assert np.max(err) < 4 * lsb
    # Decays toward zero (no gross instability).
    assert np.max(np.abs(res.y_fixed_float[-64:])) < 8 * lsb
    assert np.all(np.abs(res.y_fixed_float) <= 1.0)


def test_fixed_q31_more_accurate_than_q15():
    sos = butter_lowpass(6, 0.15)
    n = 1024
    x = np.zeros(n)
    x[0] = 1.0
    ref = filter_float(x, sos)
    r15 = filter_fixed(x, sos, FilterConfig(q_coef=QFormat(2, 14)))
    r31 = filter_fixed(
        x, sos, FilterConfig(q_sig=Q31, q_coef=QFormat(2, 30), guard_bits=0)
    )
    e15 = np.max(np.abs(r15.y_fixed_float - ref))
    e31 = np.max(np.abs(r31.y_fixed_float - ref))
    assert e31 < e15


def test_fixed_output_stays_within_signal_rail_under_overscale_input():
    sos = butter_lowpass(4, 0.1)
    t = np.arange(2000)
    x = 8.0 * np.sin(2 * np.pi * 0.03 * t)  # 8x over full scale
    res = filter_fixed(x, sos)
    assert np.all(res.y_fixed_float >= -1.0)
    assert np.all(res.y_fixed_float <= 1.0)
    assert res.input_saturations > 0
    assert res.total_state_saturations + res.output_saturations > 0


def test_saturation_fed_back_is_bounded():
    # Even an unstable linear section cannot escape the rails under saturation.
    sos = SOS(np.array([[1.0, 0.0, 0.0, 1.0, -1.5, 0.0]]))  # pole at 1.5
    x = np.zeros(300)
    x[0] = 0.5
    res = filter_fixed(x, sos)
    assert np.all(np.isfinite(res.y_fixed_float))
    assert np.max(np.abs(res.y_fixed_float)) <= 1.0


def test_truncation_biases_vs_convergent():
    sos = butter_lowpass(2, 0.2)
    n = 400
    x = np.zeros(n)
    x[0] = 1.0
    rt = filter_fixed(x, sos, FilterConfig(rounding="truncate"))
    rc = filter_fixed(x, sos, FilterConfig(rounding="convergent"))
    assert np.max(np.abs(rc.y_fixed_float - rc.y_reference)) <= np.max(
        np.abs(rt.y_fixed_float - rt.y_reference)
    ) + 1e-12


def test_config_validation():
    with pytest.raises(ValueError):
        FilterConfig(q_sig=Q31, q_coef=QFormat(2, 31))  # products exceed int64
    with pytest.raises(ValueError):
        FilterConfig(guard_bits=-1)
    with pytest.raises(ValueError):
        FilterConfig(rounding="bogon")


def test_quantized_coefficients_dequantize():
    sos = butter_lowpass(2, 0.3)
    q = sos.quantize(QFormat(2, 14))
    back = q.to_float_sos()
    assert np.max(np.abs(back.sections - sos.sections)) < 1e-4
