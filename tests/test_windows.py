"""分块窗口与逐窗估计的测试。"""

import numpy as np
import pytest

from delay_correlator.config import EstimatorConfig
from delay_correlator.signals import SyntheticSpec, make_delayed_pair
from delay_correlator.windows import estimate_windows, iter_windows


def test_iter_windows_non_overlapping():
    assert iter_windows(100, 30, 30) == [(0, 30), (30, 60), (60, 90)]


def test_iter_windows_overlapping_keeps_full_windows_only():
    # 0,20,40,60 起始都能切出完整 30 点窗；80+30>100 丢弃。
    assert iter_windows(100, 30, 20) == [(0, 30), (20, 50), (40, 70), (60, 90)]


def test_iter_windows_rejects_bad_params():
    with pytest.raises(ValueError):
        iter_windows(100, 0, 10)
    with pytest.raises(ValueError):
        iter_windows(100, 10, 0)


def test_windows_split_detected_per_block():
    """4000 样本信号、1000 样本窗：两段不同延迟应分别检出。"""
    spec = SyntheticSpec(kind="noise", duration=1.0, sample_rate=4000, seed=5)
    from delay_correlator.signals import generate_source, shift_signal

    base = generate_source(spec)
    chan = np.concatenate(
        (shift_signal(base[:2000], 11), shift_signal(base[2000:], -7))
    )
    cfg = EstimatorConfig(max_lag=64, window_size=1000)
    results = estimate_windows(base, chan, cfg)
    assert [w.index for w in results] == [0, 1, 2, 3]
    assert results[0].estimate.delay_samples == pytest.approx(11)
    assert results[1].estimate.delay_samples == pytest.approx(11)
    assert results[2].estimate.delay_samples == pytest.approx(-7)
    assert results[3].estimate.delay_samples == pytest.approx(-7)


def test_windows_mark_silent_block_uncertain():
    """4 个窗中第 3 窗静音：只有该窗状态为 uncertain，其余仍可信。"""
    spec = SyntheticSpec(kind="noise", duration=1.0, sample_rate=4000,
                         delay_samples=5, seed=8)
    ref, chan = make_delayed_pair(spec)
    chan[2000:3000] = 0.0
    ref[2000:3000] = 0.0
    cfg = EstimatorConfig(max_lag=64, window_size=1000, min_rms_db=-60.0)
    results = estimate_windows(ref, chan, cfg)
    assert len(results) == 4
    statuses = [w.estimate.confident for w in results]
    assert statuses == [True, True, False, True]
    assert "low_energy" in results[2].estimate.reasons


def test_to_dict_seconds_and_null_handling():
    spec = SyntheticSpec(kind="noise", duration=0.5, sample_rate=8000,
                         delay_samples=4, seed=2)
    ref, chan = make_delayed_pair(spec)
    results = estimate_windows(ref, chan, EstimatorConfig(max_lag=64))
    d = results[0].to_dict(sample_rate=8000)
    assert d["status"] == "confident"
    assert d["delay_seconds"] == pytest.approx(4 / 8000)
    assert "reasons" not in d

    zeros = [np.zeros(512), np.zeros(512)]
    from delay_correlator.windows import WindowEstimate
    from delay_correlator.correlator import estimate_lag
    est = estimate_lag(zeros[0], zeros[1], EstimatorConfig(max_lag=64))
    silent = WindowEstimate(0, 0, 512, est).to_dict(sample_rate=None)
    assert silent["rms_db"] is None  # -inf 不可进 JSON
    assert silent["delay_seconds"] is None
    # 全零窗必然 low_energy；低峰/边界等附加原因码不做硬性要求。
    assert "low_energy" in silent["reasons"]
    assert silent["status"] == "uncertain"


def test_config_rejects_window_smaller_than_search_range():
    with pytest.raises(ValueError):
        EstimatorConfig(max_lag=200, window_size=100)
    with pytest.raises(ValueError):
        EstimatorConfig(max_lag=64, hop_size=100)  # 有 hop 无 window
