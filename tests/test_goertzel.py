import math

import numpy as np
import pytest

from dtmf.goertzel import estimate_amplitude, goertzel_power, goertzel_power_batch
from dtmf.tones import ALL_FREQS

FS = 8000


def sine(freq, n, amp=1.0, fs=FS):
    t = np.arange(n) / fs
    return amp * np.sin(2 * np.pi * freq * t)


def test_goertzel_peak_at_target_frequency():
    x = sine(697.0, 320)
    powers = [goertzel_power(x, f, FS) for f in ALL_FREQS]
    assert ALL_FREQS[int(np.argmax(powers))] == 697.0


def test_goertzel_rejects_absent_frequency():
    x = sine(697.0, 320)
    p_hit = goertzel_power(x, 697.0, FS)
    p_miss = goertzel_power(x, 1633.0, FS)
    assert p_hit > 100 * p_miss


def test_batch_matches_recursive():
    rng = np.random.default_rng(0)
    frames = rng.normal(size=(5, 320))
    batch = goertzel_power_batch(frames, ALL_FREQS, FS)
    for i in range(frames.shape[0]):
        for j, f in enumerate(ALL_FREQS):
            ref = goertzel_power(frames[i], f, FS)
            assert batch[i, j] == pytest.approx(ref, rel=1e-9)


def test_batch_accepts_1d_input():
    x = sine(1209.0, 320)
    powers = goertzel_power_batch(x, ALL_FREQS, FS)
    assert powers.shape == (len(ALL_FREQS),)
    assert ALL_FREQS[int(np.argmax(powers))] == 1209.0


def test_estimate_amplitude_recovers_sine_amplitude():
    # 700Hz 在 320 点窗内恰好整数周期，无泄漏，可精确还原幅度
    x = sine(700.0, 320, amp=0.7)
    p = goertzel_power(x, 700.0, FS)
    assert estimate_amplitude(p, 320) == pytest.approx(0.7, rel=1e-6)
    # 非整数周期时存在泄漏，幅度估计有少量偏差（仍在 1% 内）
    x = sine(852.0, 320, amp=0.7)
    p = goertzel_power(x, 852.0, FS)
    assert estimate_amplitude(p, 320) == pytest.approx(0.7, rel=1e-2)


def test_goertzel_tracks_frequency_deviation():
    # 频偏 1% 时目标频点功率仍远高于其他频点
    x = sine(697.0 * 1.01, 320)
    powers = [goertzel_power(x, f, FS) for f in ALL_FREQS]
    assert ALL_FREQS[int(np.argmax(powers))] == 697.0
