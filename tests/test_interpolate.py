"""插值估计器单元测试：直接构造加窗正弦，验证亚频点偏移估计。"""

import numpy as np
import pytest

from spectral_peak.interpolate import (
    hann_exact_delta,
    log_parabolic_delta,
    quinn_delta,
    hann_amplitude_correction,
    resolve_method,
)
from spectral_peak.windows import get_window

N = 4096
FS = 48000.0


def _windowed_rfft(freq_hz, window_name):
    t = np.arange(N) / FS
    x = np.sin(2.0 * np.pi * freq_hz * t)
    w = get_window(window_name, N)
    X = np.fft.rfft(x * w)
    return X, np.abs(X)


@pytest.mark.parametrize("offset", [-0.45, -0.2, 0.0, 0.13, 0.37, 0.49])
def test_hann_exact_delta_recovers_offset(offset):
    k0 = 100
    X, mag = _windowed_rfft((k0 + offset) * FS / N, "hann")
    delta = hann_exact_delta(mag, k0)
    assert abs(delta - offset) < 0.01


@pytest.mark.parametrize("offset", [-0.3, 0.0, 0.25, 0.45])
def test_log_parabolic_delta_close_to_offset(offset):
    k0 = 100
    X, mag = _windowed_rfft((k0 + offset) * FS / N, "hann")
    delta = log_parabolic_delta(mag, k0)
    # 对数抛物线用于 Hann 窗是近似，容差放宽
    assert abs(delta - offset) < 0.05


@pytest.mark.parametrize("offset", [-0.4, 0.1, 0.35])
def test_quinn_delta_rectangular(offset):
    k0 = 100
    X, mag = _windowed_rfft((k0 + offset) * FS / N, "rectangular")
    delta = quinn_delta(X, k0)
    assert abs(delta - offset) < 0.02


def test_hann_amplitude_correction_bounds():
    assert hann_amplitude_correction(0.0) == pytest.approx(1.0)
    assert hann_amplitude_correction(0.5) == pytest.approx(np.pi * 0.5 * 0.75, rel=1e-9)
    assert hann_amplitude_correction(-0.3) == hann_amplitude_correction(0.3)


def test_resolve_method():
    assert resolve_method("auto", "hann") == "hann-exact"
    assert resolve_method("auto", "rectangular") == "log-parabolic"
    assert resolve_method("quinn", "hann") == "quinn"
    with pytest.raises(ValueError):
        resolve_method("bogus", "hann")
