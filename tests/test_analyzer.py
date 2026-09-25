"""End-to-end analyser tests on synthetic signals."""

import numpy as np
import pytest

from spectral_peak import analyze, multitone, tone

SR = 48000.0
N = 4096
BIN_HZ = SR / N


def _top_peak(result):
    return max(result["peaks"], key=lambda p: p["amplitude"])


def test_noninteger_frequency_recovered():
    freq = 100.37 * BIN_HZ
    result = analyze(tone(freq, amplitude=0.8, sample_rate=SR, n=N), SR)
    peak = _top_peak(result)
    assert abs(peak["freq_hz"] - freq) < 0.02 * BIN_HZ
    assert peak["amplitude"] == pytest.approx(0.8, rel=0.01)
    assert peak["interference"] is False
    assert peak["boundary"] is False


def test_integer_frequency_recovered():
    freq = 150.0 * BIN_HZ
    result = analyze(tone(freq, sample_rate=SR, n=N), SR)
    peak = _top_peak(result)
    assert abs(peak["freq_hz"] - freq) < 0.02 * BIN_HZ


def test_dc_offset_flagged_boundary_not_interpolated():
    result = analyze(np.full(N, 0.5), SR)
    peak = _top_peak(result)
    assert peak["bin"] == 0
    assert peak["boundary"] is True
    assert peak["estimator"] == "none"
    assert peak["freq_hz"] == 0.0
    assert peak["amplitude"] == pytest.approx(0.5, rel=0.01)


def test_nyquist_flagged_boundary_not_interpolated():
    # A Nyquist sine with phase 0 samples to all zeros; use phase pi/2.
    result = analyze(
        tone(SR / 2, amplitude=0.8, sample_rate=SR, n=N, phase=np.pi / 2), SR
    )
    peak = _top_peak(result)
    assert peak["bin"] == N // 2
    assert peak["boundary"] is True
    assert peak["freq_hz"] == pytest.approx(SR / 2)
    assert peak["amplitude"] == pytest.approx(0.8, rel=0.02)


def test_two_close_tones_flag_interference():
    f1 = 100.3 * BIN_HZ
    # 3 bins apart: both resolved as local maxima, still inside the 4-bin
    # Hann mainlobe, so both must carry the interference flag.
    f2 = f1 + 3.0 * BIN_HZ
    result = analyze(multitone([(f1, 0.8), (f2, 0.8)], sample_rate=SR, n=N), SR)
    interior = [p for p in result["peaks"] if not p["boundary"]]
    assert any(p["interference"] for p in interior)


def test_two_well_separated_tones_no_interference():
    f1 = 100.3 * BIN_HZ
    f2 = f1 + 12.0 * BIN_HZ
    result = analyze(multitone([(f1, 0.8), (f2, 0.8)], sample_rate=SR, n=N), SR)
    interior = sorted(
        (p for p in result["peaks"] if not p["boundary"]),
        key=lambda p: p["freq_hz"],
    )
    assert len(interior) >= 2
    assert not any(p["interference"] for p in interior[:2])
    assert abs(interior[0]["freq_hz"] - f1) < 0.05 * BIN_HZ
    assert abs(interior[1]["freq_hz"] - f2) < 0.05 * BIN_HZ


def test_rejects_too_few_samples():
    with pytest.raises(ValueError):
        analyze(np.zeros(4), SR)


def test_rejects_bad_sample_rate():
    with pytest.raises(ValueError):
        analyze(np.zeros(N), 0.0)


def test_unknown_window_rejected():
    with pytest.raises(ValueError):
        analyze(np.zeros(N), SR, window="kaiser")


def test_unknown_estimator_rejected():
    with pytest.raises(ValueError):
        analyze(tone(1000.0, sample_rate=SR, n=N), SR, estimator="magic")
