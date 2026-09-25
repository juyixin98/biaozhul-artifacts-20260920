"""Unit tests for the DTMF detector and sequence decoder."""

import numpy as np
import pytest

from dtmf_service.detector import DetectorConfig, DTMFDetector
from dtmf_service.synth import ALL_KEYS, synthesize_sequence, synthesize_tone

RATE = 8000


def detect(samples, **cfg_overrides):
    cfg = DetectorConfig(sample_rate=RATE, **cfg_overrides)
    return DTMFDetector(cfg).detect(samples, RATE)


@pytest.mark.parametrize("key", list(ALL_KEYS))
def test_each_key_detected_clean(key):
    samples = synthesize_sequence(key, RATE, tone_ms=100, gap_ms=50, seed=1)
    result = detect(samples)
    assert result.digits == key
    assert len(result.events) == 1
    assert result.events[0].key == key


def test_sequence_decoded():
    samples = synthesize_sequence("159#", RATE, seed=2)
    result = detect(samples)
    assert result.digits == "159#"
    starts = [e.start_ms for e in result.events]
    assert starts == sorted(starts)


def test_silence_yields_empty():
    result = detect(np.zeros(RATE))
    assert result.digits == ""
    assert result.rejections == ()


def test_short_tone_rejected():
    samples = synthesize_sequence("5", RATE, tone_ms=20, gap_ms=50, seed=3)
    result = detect(samples)
    assert result.digits == ""
    assert any(r.reason == "tone_too_short" for r in result.rejections)


def test_white_noise_rejected():
    rng = np.random.default_rng(4)
    result = detect(rng.normal(0, 0.3, RATE))
    assert result.digits == ""


def test_single_tone_rejected():
    t = np.arange(int(RATE * 0.1)) / RATE
    samples = np.sin(2 * np.pi * 697.0 * t) * 0.5  # low tone only, no high
    result = detect(samples)
    assert result.digits == ""


def test_extreme_twist_rejected():
    samples = synthesize_sequence("5", RATE, twist_db=20.0, seed=5)
    result = detect(samples)
    assert result.digits == ""
    assert any(r.reason == "twist_out_of_range" for r in result.rejections)


def test_same_key_twice_with_long_gap():
    samples = synthesize_sequence("77", RATE, tone_ms=80, gap_ms=100, seed=6)
    assert detect(samples).digits == "77"


def test_same_key_short_gap_merges():
    # Gap below min_gap_ms (30 ms) is treated as a detection dropout,
    # not a re-press.
    samples = synthesize_sequence("77", RATE, tone_ms=80, gap_ms=10, seed=6)
    assert detect(samples).digits == "7"


def test_adjacent_keys_switching():
    # Back-to-back different keys with a minimal gap must both decode.
    samples = synthesize_sequence("12", RATE, tone_ms=80, gap_ms=40, seed=7)
    assert detect(samples).digits == "12"


def test_result_to_dict_shape():
    samples = synthesize_sequence("0", RATE, seed=8)
    d = detect(samples).to_dict()
    assert set(d) == {"digits", "events", "rejections", "sample_rate", "n_samples"}
    assert d["digits"] == "0"
    assert set(d["events"][0]) == {
        "key", "start_ms", "end_ms", "low_freq", "high_freq", "twist_db",
    }
