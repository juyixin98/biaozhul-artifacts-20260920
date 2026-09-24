"""单窗口估计测试：整数/分数延迟、置信度、不确定状态与边界。"""

import json

import numpy as np
import pytest

from conftest import fractional_shift
from delay_correlator.estimate import estimate_window, local_maxima
from delay_correlator.signals import chirp, shift_signal, sine, white_noise


# ---------- 已知整数延迟 ----------

@pytest.mark.parametrize("delay", [0, 1, 3, 17, 99])
def test_positive_integer_delays(rng, delay):
    a = white_noise(4000, seed=7)
    b = shift_signal(a, delay) + rng.normal(scale=1e-3, size=4000)
    r = estimate_window(a, b, max_lag=120, sample_rate=8000)
    assert r.status == "ok"
    assert r.lag_samples == delay
    assert r.peak_coherence > 0.99
    assert r.lag_seconds == pytest.approx(delay / 8000, abs=1e-12)


@pytest.mark.parametrize("delay", [-1, -5, -17, -100])
def test_negative_integer_delays(rng, delay):
    a = white_noise(4000, seed=8)
    b = shift_signal(a, delay) + rng.normal(scale=1e-3, size=4000)
    r = estimate_window(a, b, max_lag=120)
    assert r.status == "ok"
    assert r.lag_samples == delay  # 负延迟必须报负号


def test_zero_delay_is_not_boundary(rng):
    a = white_noise(2000, seed=9)
    r = estimate_window(a, a + rng.normal(scale=1e-4, size=2000), max_lag=50)
    assert r.lag_samples == 0
    assert not r.at_boundary


# ---------- 分数延迟（抛物线插值）----------

def test_fractional_lag_parabolic(rng):
    a = white_noise(8000, amplitude=1.0, seed=11)
    b = fractional_shift(a, 12.4) + rng.normal(scale=1e-4, size=8000)
    r = estimate_window(a, b, max_lag=100)
    assert r.lag_samples == 12
    assert r.fractional_lag is not None
    assert r.fractional_lag == pytest.approx(12.4, abs=0.15)


# ---------- 静音 / 低能量 / 噪声 ----------

def test_silence_is_uncertain_low_energy():
    z = np.zeros(2000)
    r = estimate_window(z, z, max_lag=50)
    assert r.status == "uncertain"
    assert "low_energy" in r.uncertainty_reasons


def test_tiny_signal_near_energy_floor_is_uncertain():
    # RMS ≈ 1e-4（约 -80 dBFS），远低于默认 -60 dBFS 阈值
    a = np.full(2000, 1e-4) + (np.arange(2000) % 2) * 1e-4
    r = estimate_window(a, a.copy(), max_lag=20)
    assert r.status == "uncertain"
    assert "low_energy" in r.uncertainty_reasons


def test_uncorrelated_noise_low_coherence(rng):
    a = rng.normal(size=8000)
    b = rng.normal(size=8000)
    r = estimate_window(a, b, max_lag=200, min_coherence=0.5)
    assert r.status == "uncertain"
    assert "low_coherence" in r.uncertainty_reasons
    assert r.peak_coherence < 0.5


def test_noisy_pair_still_resolves(rng):
    """SNR=0 dB 的噪声副本仍应能定位整数延迟。"""
    a = white_noise(8000, seed=12)
    b = shift_signal(a, 25)
    power = np.mean(b ** 2)
    b = b + rng.normal(scale=np.sqrt(power), size=8000)
    r = estimate_window(a, b, max_lag=100)
    assert r.lag_samples == 25
    assert r.peak_coherence > 0.5  # 有噪声但相干性仍足够


# ---------- 周期信号：多峰不确定 ----------

def test_periodic_signal_flagged_ambiguous():
    n, sr, f = 4000, 8000, 200.0
    a = sine(n, freq=f, sample_rate=sr)
    b = shift_signal(a, 20)
    r = estimate_window(a, b, max_lag=100, ambiguity_guard=3)
    assert r.status == "uncertain"
    assert "ambiguous_peaks" in r.uncertainty_reasons
    assert r.peak_coherence == pytest.approx(1.0, abs=1e-9)
    assert r.peak_ratio == pytest.approx(1.0, abs=1e-9)
    # 报告的整数峰仍应是真值（恰好 20）
    assert r.lag_samples == 20


def test_chirp_not_ambiguous(rng):
    """扫频信号周期随时间变化，不存在整段重复峰，应正常定位。"""
    a = chirp(8000, f0=300, f1=1500, sample_rate=8000, amplitude=0.9)
    b = shift_signal(a, 33) + rng.normal(scale=1e-3, size=8000)
    r = estimate_window(a, b, max_lag=100)
    assert r.status == "ok"
    assert r.lag_samples == 33


# ---------- 边界 ----------

def test_peak_at_search_boundary_flagged():
    # 真实延迟超出搜索范围：构造使相关在搜索区间右端单调最大的信号。
    # A 为阶跃，B 为右移 60 的阶跃，只搜 ±30 时 r(k) 在 k=+30（边界）最大。
    a = np.concatenate([np.zeros(2000), np.ones(2000)])
    b = shift_signal(a, 60)
    r = estimate_window(a, b, max_lag=30)
    assert r.at_boundary
    assert r.lag_samples == 30
    assert "boundary_peak" in r.uncertainty_reasons
    assert r.status == "uncertain"
    assert r.fractional_lag is None  # 边界不做插值


def test_boundary_clear_when_delay_within_range(rng):
    a = white_noise(4000, seed=14)
    b = shift_signal(a, 10)
    r = estimate_window(a, b, max_lag=50)
    assert not r.at_boundary
    assert r.status == "ok"


# ---------- 峰查找工具 ----------

def test_local_maxima_simple():
    x = np.array([0.0, 1.0, 0.0, 2.0, 0.0])
    np.testing.assert_array_equal(local_maxima(x), np.array([1, 3]))


def test_local_maxima_plateau_midpoint():
    x = np.array([0.0, 1.0, 1.0, 1.0, 0.0])
    np.testing.assert_array_equal(local_maxima(x), np.array([2]))


def test_local_maxima_constant_is_empty():
    assert local_maxima(np.zeros(10)).size == 0


def test_local_maxima_edges():
    x = np.array([5.0, 4.0, 3.0, 2.0, 6.0])
    # 边界点在 estimate_window 内被补入候选；local_maxima 本身只找内部峰
    idx = local_maxima(x)
    assert idx.size == 0 or set(idx.tolist()) <= {0, 4}


def test_to_dict_json_friendly(rng):
    a = white_noise(1000, seed=15)
    d = estimate_window(a, shift_signal(a, 4), max_lag=20).to_dict()
    json.dumps(d)  # 不抛异常即可
    assert d["lag_samples"] == 4
