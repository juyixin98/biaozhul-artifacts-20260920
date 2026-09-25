"""峰检测主流程测试：单音精度、双音干扰标志、边界、错误处理。"""

import numpy as np
import pytest

from spectral_peak.detector import AnalysisConfig, detect_peaks
from spectral_peak.service import analyze, synthesize

FS = 48000.0
N = 4096
BIN_HZ = FS / N  # 11.71875


def _tone(freq, amp=1.0, phase=0.0):
    return {"frequency_hz": freq, "amplitude": amp, "phase": phase}


def test_single_tone_noninteger_bin_frequency_and_amplitude():
    freq = 100.37 * BIN_HZ
    samples = synthesize([_tone(freq, 0.8)], FS, N)
    result = analyze(samples, FS)
    assert len(result["peaks"]) == 1
    peak = result["peaks"][0]
    assert abs(peak["frequency_hz"] - freq) < 0.01 * BIN_HZ
    assert abs(peak["amplitude"] - 0.8) / 0.8 < 0.01
    assert peak["interference"] is False
    assert peak["boundary"] is False


def test_integer_bin_tone_delta_near_zero():
    freq = 100.0 * BIN_HZ
    samples = synthesize([_tone(freq)], FS, N)
    peak = analyze(samples, FS)["peaks"][0]
    assert abs(peak["delta_bins"]) < 0.01


def test_two_resolved_tones_flag_interference():
    f1 = 100.3 * BIN_HZ
    f2 = 105.6 * BIN_HZ  # 间距 5.3 bin，可分辨但主瓣相互污染
    samples = synthesize([_tone(f1), _tone(f2, 0.9)], FS, N)
    peaks = analyze(samples, FS)["peaks"]
    assert len(peaks) == 2
    assert all(p["interference"] for p in peaks)
    freqs = sorted(p["frequency_hz"] for p in peaks)
    assert abs(freqs[0] - f1) < 0.05 * BIN_HZ
    assert abs(freqs[1] - f2) < 0.05 * BIN_HZ


def test_two_far_tones_no_interference():
    f1 = 100.3 * BIN_HZ
    f2 = 300.7 * BIN_HZ
    samples = synthesize([_tone(f1), _tone(f2)], FS, N)
    peaks = analyze(samples, FS)["peaks"]
    assert len(peaks) == 2
    assert not any(p["interference"] for p in peaks)


def test_merged_tones_report_single_peak():
    # 间距 1.5 bin < 主瓣宽度：合并为一个峰，这是明确的适用边界
    f1 = 100.0 * BIN_HZ
    f2 = 101.5 * BIN_HZ
    samples = synthesize([_tone(f1), _tone(f2)], FS, N)
    peaks = analyze(samples, FS)["peaks"]
    assert len(peaks) == 1


def test_dc_constant_signal_boundary_peak():
    samples = np.full(N, 0.5)
    peaks = analyze(samples, FS)["peaks"]
    assert peaks[0]["bin"] == 0
    assert peaks[0]["frequency_hz"] == 0.0
    assert peaks[0]["boundary"] is True
    assert abs(peaks[0]["amplitude"] - 0.5) < 1e-9


def test_nyquist_tone_boundary_peak():
    t = np.arange(N) / FS
    samples = 0.7 * np.cos(np.pi * np.arange(N))  # 恰在 Nyquist
    peaks = analyze(samples, FS)["peaks"]
    top = peaks[0]
    assert top["bin"] == N // 2
    assert top["boundary"] is True
    assert abs(top["frequency_hz"] - FS / 2) < 1e-9
    assert abs(top["amplitude"] - 0.7) < 1e-9


def test_near_nyquist_tone_interpolated():
    # 距 Nyquist 3.4 bin：镜像泄漏可忽略，精度恢复正常
    freq = (N / 2 - 3.4) * BIN_HZ
    samples = synthesize([_tone(freq)], FS, N)
    peak = analyze(samples, FS)["peaks"][0]
    assert abs(peak["frequency_hz"] - freq) < 0.01 * BIN_HZ
    assert peak["boundary"] is False


def test_very_near_nyquist_tone_degraded_but_bounded():
    # 距 Nyquist 仅 1.4 bin：负频率镜像主瓣重叠，三点插值物理性退化。
    # 此处不断言高精度，只断言误差不发散（如实记录退化区行为）。
    freq = (N / 2 - 1.4) * BIN_HZ
    samples = synthesize([_tone(freq)], FS, N)
    peak = analyze(samples, FS)["peaks"][0]
    assert abs(peak["frequency_hz"] - freq) < 0.2 * BIN_HZ


def test_near_dc_tone_interpolated():
    # 距直流 3.4 bin：镜像泄漏可忽略，精度恢复正常
    freq = 3.4 * BIN_HZ
    samples = synthesize([_tone(freq)], FS, N)
    peak = analyze(samples, FS)["peaks"][0]
    assert abs(peak["frequency_hz"] - freq) < 0.01 * BIN_HZ


def test_very_near_dc_tone_degraded_but_bounded():
    # 距直流仅 1.4 bin：镜像泄漏导致退化，只断言误差不发散
    freq = 1.4 * BIN_HZ
    samples = synthesize([_tone(freq)], FS, N)
    peak = analyze(samples, FS)["peaks"][0]
    assert abs(peak["frequency_hz"] - freq) < 0.2 * BIN_HZ


def test_result_schema_and_metadata():
    samples = synthesize([_tone(440.0)], FS, N)
    result = analyze(samples, FS)
    for key in ("sample_rate", "n_samples", "window", "method",
                "frequency_resolution_hz", "peaks"):
        assert key in result
    assert result["method"] == "hann-exact"
    peak = result["peaks"][0]
    for key in ("rank", "bin", "delta_bins", "frequency_hz",
                "amplitude", "interference", "boundary"):
        assert key in peak
    assert peak["rank"] == 1


def test_peaks_sorted_by_amplitude_with_ranks():
    samples = synthesize([_tone(200.2 * BIN_HZ, 0.5), _tone(100.5 * BIN_HZ, 1.0)], FS, N)
    peaks = analyze(samples, FS)["peaks"]
    assert [p["rank"] for p in peaks] == [1, 2]
    assert peaks[0]["amplitude"] >= peaks[1]["amplitude"]


def test_too_few_samples_raises():
    with pytest.raises(ValueError):
        analyze(np.zeros(4), FS)


def test_invalid_sample_rate_raises():
    with pytest.raises(ValueError):
        analyze(np.zeros(N), 0.0)


def test_silence_returns_no_peaks():
    assert analyze(np.zeros(N), FS)["peaks"] == []


def test_rectangular_window_with_quinn():
    freq = 100.37 * BIN_HZ
    samples = synthesize([_tone(freq)], FS, N)
    result = analyze(samples, FS, window="rectangular", method="quinn")
    peak = result["peaks"][0]
    assert abs(peak["frequency_hz"] - freq) < 0.02 * BIN_HZ
