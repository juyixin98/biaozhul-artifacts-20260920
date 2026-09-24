"""相关器单元测试：FFT 正确性、归一化、符号约定、边界。"""

import numpy as np
import pytest

from delay_correlator.correlator import normalized_xcorr, xcorr_full
from delay_correlator.signals import shift_signal


def direct_corr(a, b, max_lag):
    """按项目约定 r[d] = Σ a[n] b[n+d] 的直接实现（慢但直观）。"""
    n = len(a)
    lags = np.arange(-max_lag, max_lag + 1)
    out = []
    for d in lags:
        ia_lo, ia_hi = max(0, -d), n - max(0, d)
        ib_lo, ib_hi = max(0, d), n - max(0, -d)
        out.append(np.dot(a[ia_lo:ia_hi], b[ib_lo:ib_hi]))
    return lags, np.array(out)


def test_xcorr_full_matches_bruteforce(rng):
    a = rng.normal(size=64)
    b = rng.normal(size=64)
    c = xcorr_full(a, b)
    lags, ref = direct_corr(a, b, 63)
    assert c.shape == (127,)
    np.testing.assert_allclose(c[lags + 63], ref, atol=1e-10)


def test_zero_lag_autocorrelation_is_energy(rng):
    a = rng.normal(size=100)
    c = xcorr_full(a, a)
    assert c[99] == pytest.approx(np.dot(a, a))


def test_sign_convention_positive_delay_peak(rng):
    """B[n] = A[n-d]（d>0，B 更晚）必须在 +d 处出峰。"""
    a = rng.normal(size=3000)
    b = shift_signal(a, 42)
    prof = normalized_xcorr(a, b, max_lag=100)
    assert prof.lags[np.argmax(prof.corr)] == 42


def test_sign_convention_negative_delay_peak(rng):
    """d<0（B 更早）必须在负滞后出峰，验证符号未取反。"""
    a = rng.normal(size=3000)
    b = shift_signal(a, -42)
    prof = normalized_xcorr(a, b, max_lag=100)
    assert prof.lags[np.argmax(prof.corr)] == -42


def test_swapping_channels_negates_lag(rng):
    a = rng.normal(size=2000)
    b = shift_signal(a, 13)
    p1 = normalized_xcorr(a, b, 60)
    p2 = normalized_xcorr(b, a, 60)
    assert p1.lags[np.argmax(p1.corr)] == -p2.lags[np.argmax(p2.corr)]


def test_corr_values_bounded(rng):
    a = rng.normal(size=500)
    b = rng.normal(size=500)
    prof = normalized_xcorr(a, b, max_lag=200)
    assert np.all(prof.corr <= 1.0 + 1e-12)
    assert np.all(prof.corr >= -1.0 - 1e-12)


def test_autocorrelation_one_at_zero_lag(rng):
    a = rng.normal(size=1000)
    prof = normalized_xcorr(a, a, max_lag=50)
    idx0 = np.where(prof.lags == 0)[0][0]
    assert prof.corr[idx0] == pytest.approx(1.0, abs=1e-12)


def test_anticorrelation_negative_one():
    a = np.array([1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0])
    b = -a
    prof = normalized_xcorr(a, b, max_lag=0)
    assert prof.corr[0] == pytest.approx(-1.0, abs=1e-12)


def test_zncc_robust_to_dc_offset(rng):
    a = rng.normal(size=2000)
    b = shift_signal(a, 9) + 5.0  # B 带强直流
    prof = normalized_xcorr(a, b, max_lag=50, method="zncc")
    assert prof.corr.max() > 0.999
    # 去均值前的普通能量归一化会被直流严重拉低
    prof_ncc = normalized_xcorr(a, b, max_lag=50, method="ncc")
    assert prof_ncc.corr.max() < 0.95


def test_constant_signals_corr_zero_no_nan():
    a = np.ones(500)
    b = np.ones(500) * 3.0
    prof = normalized_xcorr(a, b, max_lag=50, method="zncc")
    assert not np.any(np.isnan(prof.corr))
    np.testing.assert_allclose(prof.corr, 0.0)


def test_max_lag_clipped_and_lags_axis(rng):
    a = rng.normal(size=100)
    prof = normalized_xcorr(a, a, max_lag=500)  # 超过 N-1
    assert prof.lags[0] == -99
    assert prof.lags[-1] == 99
    assert prof.lags.size == 199


def test_overlap_lengths(rng):
    a = rng.normal(size=100)
    prof = normalized_xcorr(a, a, max_lag=10)
    expected = np.array([100 - abs(d) for d in prof.lags], dtype=float)
    np.testing.assert_array_equal(prof.overlap, expected)


def test_rejects_unequal_lengths():
    with pytest.raises(ValueError):
        normalized_xcorr(np.zeros(10), np.zeros(11), max_lag=5)


def test_rejects_bad_method(rng):
    a = rng.normal(size=50)
    with pytest.raises(ValueError):
        normalized_xcorr(a, a, max_lag=5, method="bogus")


def test_rejects_negative_max_lag(rng):
    a = rng.normal(size=50)
    with pytest.raises(ValueError):
        normalized_xcorr(a, a, max_lag=-1)
