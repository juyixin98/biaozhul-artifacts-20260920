"""窗函数与杂项边界测试。"""

import numpy as np
import pytest

from spectral_peak.windows import get_window, coherent_gain, SUPPORTED_WINDOWS
from spectral_peak.service import analyze, synthesize

FS = 48000.0
N = 4096
BIN_HZ = FS / N


def test_all_supported_windows_usable():
    for name in SUPPORTED_WINDOWS:
        w = get_window(name, N)
        assert w.shape == (N,)
        assert 0.0 < coherent_gain(w) <= 1.0
        freq = 100.37 * BIN_HZ
        samples = synthesize([{"frequency_hz": freq}], FS, N)
        peaks = analyze(samples, FS, window=name)["peaks"]
        assert len(peaks) >= 1
        # 各窗都应把主峰定位到 0.5 bin 以内
        assert abs(peaks[0]["frequency_hz"] - freq) < 0.5 * BIN_HZ


def test_blackmanharris_log_parabolic_accuracy():
    freq = 100.37 * BIN_HZ
    samples = synthesize([{"frequency_hz": freq}], FS, N)
    peak = analyze(samples, FS, window="blackmanharris",
                   method="log-parabolic")["peaks"][0]
    assert abs(peak["frequency_hz"] - freq) < 0.05 * BIN_HZ


def test_unknown_window_raises():
    with pytest.raises(ValueError):
        get_window("kaiser", N)
    with pytest.raises(ValueError):
        get_window("hann", 0)


def test_degenerate_spectrum_interpolators_return_zero():
    # 全零频谱：各估计器应安全返回 0 而不是抛异常
    from spectral_peak.interpolate import (
        hann_exact_delta, log_parabolic_delta, quinn_delta,
    )
    mag = np.zeros(16)
    X = np.zeros(16, dtype=complex)
    assert hann_exact_delta(mag, 8) == 0.0
    assert log_parabolic_delta(mag, 8) == 0.0
    assert quinn_delta(X, 8) == 0.0
