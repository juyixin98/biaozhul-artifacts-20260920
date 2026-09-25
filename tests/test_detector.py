import numpy as np
import pytest

from dtmf.detector import DetectorConfig, DtmfDetector
from dtmf.synth import SynthConfig, synthesize, to_int16
from dtmf.tones import VALID_KEYS


def decode_keys(keys, synth_cfg=None, det_cfg=None):
    signal, _ = synthesize(keys, synth_cfg or SynthConfig())
    return DtmfDetector(det_cfg or DetectorConfig()).decode(to_int16(signal))


def test_decode_clean_sequence():
    result = decode_keys("1234567890*#ABCD")
    assert result.digits == "1234567890*#ABCD"


def test_each_key_individually():
    for key in VALID_KEYS:
        assert decode_keys(key).digits == key


def test_silence_yields_empty():
    result = DtmfDetector().decode(np.zeros(8000, dtype=np.int16))
    assert result.digits == ""
    assert result.events == []


def test_pure_noise_rejected():
    rng = np.random.default_rng(42)
    noise = (rng.normal(0, 3000, 8000)).astype(np.int16)
    result = DtmfDetector().decode(noise)
    assert result.digits == ""


def test_short_tone_rejected_by_duration():
    # 20ms 音长 < 默认 40ms 最短时长 → 拒识（帧级拒识或音段过短均可）
    result = decode_keys("7", SynthConfig(tone_ms=20, gap_ms=20))
    assert result.digits == ""
    assert result.short_segments + result.rejected_frames > 0


def test_short_segment_counted():
    # 60ms 干净音可稳定成段，但 min_tone_ms=100 时音段过短被丢弃
    result = decode_keys("7", SynthConfig(tone_ms=60),
                         DetectorConfig(min_tone_ms=100))
    assert result.digits == ""
    assert result.short_segments >= 1


def test_min_tone_ms_configurable():
    result = decode_keys("7", SynthConfig(tone_ms=30, gap_ms=20),
                         DetectorConfig(min_tone_ms=20))
    assert result.digits == "7"


def test_excessive_twist_rejected():
    # twist 20dB 超过默认 8dB 上限 → 拒识
    result = decode_keys("5", SynthConfig(twist_db=20.0))
    assert result.digits == ""


def test_low_amplitude_below_noise_gate_rejected():
    result = decode_keys("5", SynthConfig(amplitude=0.005))
    assert result.digits == ""


def test_adjacent_switch_without_gap():
    # 相邻音直接切换（间隔 0ms）仍应逐个识别
    result = decode_keys("121212", SynthConfig(gap_ms=0, tone_ms=60))
    assert result.digits == "121212"


def test_decode_with_moderate_noise():
    result = decode_keys("888", SynthConfig(snr_db=20, seed=7))
    assert result.digits == "888"


def test_event_timestamps_ordered():
    result = decode_keys("159")
    starts = [e.start_ms for e in result.events]
    assert starts == sorted(starts)
    assert all(e.end_ms > e.start_ms for e in result.events)
