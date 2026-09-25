import json

import numpy as np
import pytest

from dtmf.pcmio import read_pcm, write_pcm
from dtmf.service import handle_request
from dtmf.synth import SynthConfig, synthesize, to_int16


def test_pcm_roundtrip(tmp_path):
    signal, fs = synthesize("12", SynthConfig(seed=0))
    samples = to_int16(signal)
    p = tmp_path / "a.pcm"
    write_pcm(p, samples, fs)
    back, back_fs = read_pcm(p, fs)
    assert back_fs == fs
    assert np.array_equal(back, samples)


def test_wav_roundtrip(tmp_path):
    signal, fs = synthesize("3", SynthConfig(seed=0))
    samples = to_int16(signal)
    p = tmp_path / "a.wav"
    write_pcm(p, samples, fs)
    back, back_fs = read_pcm(p)
    assert back_fs == fs
    assert np.array_equal(back, samples)


def test_raw_pcm_requires_sample_rate(tmp_path):
    p = tmp_path / "a.pcm"
    p.write_bytes(b"\x00\x00" * 100)
    with pytest.raises(ValueError):
        read_pcm(p)


def test_service_decode_synth_matched():
    resp = handle_request({"action": "decode_synth", "keys": "159#",
                           "synth": {"seed": 1, "snr_db": 25}})
    assert resp["ok"] and resp["matched"] and resp["digits"] == "159#"


def test_service_synthesize_then_decode(tmp_path):
    out = tmp_path / "svc.pcm"
    r1 = handle_request({"action": "synthesize", "keys": "42",
                         "out_file": str(out), "config": {"seed": 2}})
    assert r1["ok"] and out.exists()
    r2 = handle_request({"action": "decode", "pcm_file": str(out),
                         "sample_rate": 8000})
    assert r2["ok"] and r2["digits"] == "42"


def test_service_unknown_action():
    with pytest.raises(ValueError):
        handle_request({"action": "bogus"})


def test_service_rejects_unknown_config_field():
    with pytest.raises(ValueError):
        handle_request({"action": "decode_synth", "keys": "1",
                        "synth": {"not_a_field": 1}})
