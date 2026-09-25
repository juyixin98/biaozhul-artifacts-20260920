"""NCC 与延迟估计核心的单元测试。

覆盖验收要点：已知整数延迟（正/负）、符号定义、周期信号多峰、
静音低能量、噪声鲁棒、弱相关低峰、边界命中、亚采样插值与 Pearson 去均值。
"""

import numpy as np
import pytest

from delay_correlator.config import EstimatorConfig
from delay_correlator.correlator import (
    estimate_lag,
    normalized_cross_correlation,
)
from delay_correlator.signals import SyntheticSpec, generate_source, make_delayed_pair, shift_signal


def _ncc_reference(ref, chan, max_lag, subtract_mean=True):
    """NCC 的朴素双重循环参考实现，用于交叉验证向量化切片实现。"""
    n = ref.size
    out = []
    for d in range(-max_lag, max_lag + 1):
        if d >= 0:
            a, b = ref[: n - d], chan[d:]
        else:
            a, b = ref[-d:], chan[: n + d]
        if subtract_mean:
            a = a - a.mean()
            b = b - b.mean()
        denom = np.sqrt(np.dot(a, a) * np.dot(b, b))
        out.append(np.dot(a, b) / denom if denom > 0 else 0.0)
    return np.array(out)


def test_ncc_matches_reference_implementation():
    rng = np.random.default_rng(42)
    ref = rng.standard_normal(200)
    chan = shift_signal(ref, 17) + 0.01 * rng.standard_normal(200)
    fast = normalized_cross_correlation(ref, chan, 32)
    ref_impl = _ncc_reference(ref, chan, 32)
    np.testing.assert_allclose(fast, ref_impl, rtol=1e-12, atol=1e-12)


def test_ncc_zero_lag_is_one_for_identical_signals():
    rng = np.random.default_rng(1)
    x = rng.standard_normal(128)
    corr = normalized_cross_correlation(x, x, 16)
    assert corr[16] == pytest.approx(1.0, abs=1e-12)
    assert np.max(np.abs(corr)) == pytest.approx(1.0, abs=1e-12)


def test_ncc_pearson_immune_to_dc_offset_and_gain():
    """去均值 NCC 对直流偏置和固定增益不敏感：峰仍在零延迟且接近 1。"""
    rng = np.random.default_rng(7)
    x = rng.standard_normal(256)
    y = 3.0 * x + 5.0  # 增益 + 直流
    corr = normalized_cross_correlation(x, y, 20, subtract_mean=True)
    assert int(np.argmax(corr)) - 20 == 0
    assert corr[20] > 0.9999


def test_positive_integer_delay_white_noise():
    """验收：已知正整数延迟的白噪声必须精确检出且状态可信。"""
    spec = SyntheticSpec(kind="noise", duration=0.5, sample_rate=8000,
                         delay_samples=37, seed=11, noise_db=None)
    ref, chan = make_delayed_pair(spec)
    cfg = EstimatorConfig(max_lag=128, min_peak=0.6)
    est = estimate_lag(ref, chan, cfg)
    assert est.confident, est.reasons
    assert est.delay_samples == pytest.approx(37, abs=1e-9)
    assert est.peak > 0.99
    assert est.edge_hit is False


def test_negative_integer_delay_white_noise():
    """验收：负延迟（chan 超前/左移）必须检出负值，验证符号定义。"""
    spec = SyntheticSpec(kind="noise", duration=0.5, sample_rate=8000,
                         delay_samples=-37, seed=11, noise_db=None)
    ref, chan = make_delayed_pair(spec)
    cfg = EstimatorConfig(max_lag=128, min_peak=0.6)
    est = estimate_lag(ref, chan, cfg)
    assert est.confident, est.reasons
    assert est.delay_samples == pytest.approx(-37, abs=1e-9)


def test_sign_convention_direct_shift_check():
    """直接对照约定：chan[k] = ref[k-D] 在 lag=D 处取峰，正负方向均验。"""
    rng = np.random.default_rng(3)
    x = rng.standard_normal(300)
    for d in (-25, 0, 25):
        y = shift_signal(x, d)
        corr = normalized_cross_correlation(x, y, 40)
        lag = int(np.argmax(corr)) - 40
        assert lag == d


def test_periodic_sine_is_flagged_multiple_peaks():
    """验收：周期正弦整周期重相关，次峰与主峰等高，必须判 multiple_peaks 不确定。"""
    sr = 8000
    spec = SyntheticSpec(kind="sine", duration=0.5, sample_rate=sr,
                         freq=200.0, delay_samples=10, amplitude=0.8)
    ref, chan = make_delayed_pair(spec)
    cfg = EstimatorConfig(max_lag=200, min_peak=0.5,
                          max_secondary_peak_ratio=0.85, min_peak_distance=3)
    est = estimate_lag(ref, chan, cfg)
    # 主峰本身很尖，但周期重相关制造了同等高度的候选峰。
    assert "multiple_peaks" in est.reasons
    assert est.confident is False


def test_silence_is_low_energy():
    """验收：双静音窗口必须 low_energy；NCC 无定义处不应产生 NaN。"""
    ref = np.zeros(512)
    chan = np.zeros(512)
    cfg = EstimatorConfig(max_lag=64, min_rms_db=-60.0)
    est = estimate_lag(ref, chan, cfg)
    assert est.confident is False
    assert "low_energy" in est.reasons
    assert np.isfinite(est.peak)
    assert est.rms_db == float("-inf")


def test_one_sided_silence_is_low_energy():
    """一路静音、一路有声：取较弱通道能量，同样不可信。"""
    rng = np.random.default_rng(5)
    ref = rng.standard_normal(512) * 0.5
    chan = np.zeros(512)
    est = estimate_lag(ref, chan, EstimatorConfig(max_lag=64))
    assert "low_energy" in est.reasons
    assert est.confident is False


def test_uncorrelated_noise_is_low_peak():
    """验收：两路独立噪声不相关，峰高不足，必须 low_peak 不确定。"""
    rng = np.random.default_rng(99)
    ref = rng.standard_normal(2048) * 0.5
    chan = rng.standard_normal(2048) * 0.5
    est = estimate_lag(ref, chan, EstimatorConfig(max_lag=128, min_peak=0.5))
    assert est.peak < 0.5
    assert "low_peak" in est.reasons
    assert est.confident is False


def test_white_noise_with_moderate_noise_still_confident():
    """验收：带噪（-15 dB）白噪声已知延迟仍应稳定检出。"""
    spec = SyntheticSpec(kind="noise", duration=1.0, sample_rate=8000,
                         delay_samples=23, seed=19, noise_db=-15.0)
    ref, chan = make_delayed_pair(spec)
    est = estimate_lag(ref, chan, EstimatorConfig(max_lag=128, min_peak=0.5))
    assert est.confident, est.reasons
    assert abs(est.delay_samples - 23) < 1.5  # 噪声下允许 1 个采样点级抖动
    assert 0.5 < est.peak < 1.0


def test_edge_hit_when_delay_at_search_boundary():
    """真实延迟恰好等于搜索边界 max_lag 时必须 edge_hit：边界点虽完美对齐，
    但无法排除真实延迟更大的情况，估计不可信。

    用白噪声循环移位恰好 40：lag=+40 比较的两个切片完全相同（NCC≈1），
    lag=39 因接缝处有 1 个错配点而略低，因此峰稳定贴在边界上。
    """
    rng = np.random.default_rng(31)
    n, max_lag = 2048, 64
    base = rng.standard_normal(n)
    chan = np.roll(base, max_lag)
    est = estimate_lag(base, chan, EstimatorConfig(max_lag=max_lag, min_peak=0.5))
    assert est.delay_samples == pytest.approx(float(max_lag))
    assert est.peak > 0.99
    assert est.edge_hit is True
    assert "edge_hit" in est.reasons
    assert est.confident is False


def test_interpolated_subsample_delay_on_chirp():
    """验收：亚采样延迟（2.5 样本）在非周期扫频信号上插值后偏差应小于 0.3 样本。

    用频域平移构造无混叠的亚采样移位信号（能量集中在远低于奈奎斯特频率）。
    """
    spec = SyntheticSpec(kind="chirp", duration=1.0, sample_rate=8000,
                         f0=300.0, f1=1200.0, amplitude=0.7, seed=0)
    base = generate_source(spec)
    d = 2.5
    spectrum = np.fft.fft(base)
    freqs = np.fft.fftfreq(base.size, d=1.0 / 8000)
    shifted = np.fft.ifft(spectrum * np.exp(-1j * 2 * np.pi * freqs * d / 8000)).real

    cfg = EstimatorConfig(max_lag=64, min_peak=0.8, interpolate=True)
    est = estimate_lag(base, shifted, cfg)
    assert est.confident, est.reasons
    assert abs(est.delay_samples - d) < 0.3


def test_integer_mode_delay_is_exact_without_interpolation():
    """不开启插值时，无噪白噪声的输出必须是整数值。"""
    spec = SyntheticSpec(kind="noise", duration=0.5, sample_rate=8000,
                         delay_samples=12, seed=21)
    ref, chan = make_delayed_pair(spec)
    est = estimate_lag(ref, chan, EstimatorConfig(max_lag=64, interpolate=False))
    assert float(est.delay_samples) == float(int(est.delay_samples))
    assert est.delay_samples == 12


def test_negative_peak_keeps_sign_for_antiphase():
    """反相（chan = -ref）时 NCC 峰为 -1，绝对值选峰但 peak 字段保留负号。"""
    rng = np.random.default_rng(0)
    x = rng.standard_normal(256)
    est = estimate_lag(x, -x, EstimatorConfig(max_lag=32))
    assert est.peak < -0.999
