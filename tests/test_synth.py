import numpy as np
import pytest

from dtmf.synth import SynthConfig, synthesize, to_int16
from dtmf.tones import KEY_TO_FREQS


def test_synthesize_length_matches_timing():
    cfg = SynthConfig(tone_ms=60, gap_ms=40, lead_silence_ms=20, tail_silence_ms=20)
    signal, fs = synthesize("123", cfg)
    expected_ms = 20 + 3 * 60 + 2 * 40 + 20
    assert len(signal) == int(round(expected_ms * fs / 1000))


def test_synthesize_rejects_unknown_key():
    with pytest.raises(ValueError):
        synthesize("1X2")


def test_synthesize_zero_gap_has_no_silence_between_tones():
    cfg = SynthConfig(tone_ms=50, gap_ms=0, lead_silence_ms=0, tail_silence_ms=0)
    signal, fs = synthesize("12", cfg)
    assert len(signal) == int(round(100 * fs / 1000))


def test_synthesize_snr_injection_adds_noise():
    clean, _ = synthesize("5", SynthConfig(snr_db=None))
    noisy, _ = synthesize("5", SynthConfig(snr_db=10, seed=1))
    assert not np.allclose(clean, noisy)
    residual = noisy - clean
    assert np.std(residual) > 0


def test_to_int16_clips_and_converts():
    out = to_int16(np.array([0.0, 0.5, -0.5, 2.0, -2.0]))
    assert out.dtype == np.int16
    assert out[0] == 0
    assert out[3] == 32767 and out[4] == -32767


def test_all_keys_have_distinct_freq_pairs():
    pairs = list(KEY_TO_FREQS.values())
    assert len(set(pairs)) == len(pairs) == 16
