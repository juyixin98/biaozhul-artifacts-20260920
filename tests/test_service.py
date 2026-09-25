"""Unit tests for the service layer, PCM I/O and CLI."""

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from dtmf_service.pcm import read_pcm, write_pcm
from dtmf_service.service import detect_file, handle_request
from dtmf_service.synth import synthesize_sequence

RATE = 8000


def test_pcm_roundtrip(tmp_path):
    samples = synthesize_sequence("3", RATE, seed=9)
    wav = tmp_path / "tone.wav"
    write_pcm(wav, samples, RATE)
    loaded, rate = read_pcm(wav)
    assert rate == RATE
    assert loaded.shape == samples.shape
    assert np.max(np.abs(loaded - samples)) < 1e-3


def test_raw_pcm_requires_rate(tmp_path):
    raw = tmp_path / "tone.pcm"
    raw.write_bytes(np.zeros(100, dtype="<i2").tobytes())
    with pytest.raises(ValueError):
        read_pcm(raw)
    samples, rate = read_pcm(raw, sample_rate=RATE)
    assert rate == RATE and len(samples) == 100


def test_detect_file(tmp_path):
    samples = synthesize_sequence("88", RATE, seed=10)
    wav = tmp_path / "seq.wav"
    write_pcm(wav, samples, RATE)
    assert detect_file(wav)["digits"] == "88"


def test_request_synthesize():
    result = handle_request(
        {"sample_rate": RATE, "synthesize": {"keys": "42", "seed": 11}}
    )
    assert result["digits"] == "42"


def test_request_pcm_file(tmp_path):
    wav = tmp_path / "in.wav"
    write_pcm(wav, synthesize_sequence("6", RATE, seed=12), RATE)
    result = handle_request({"pcm_file": str(wav)})
    assert result["digits"] == "6"


def test_request_rejects_ambiguous_input():
    with pytest.raises(ValueError):
        handle_request({"pcm_file": "a.wav", "synthesize": {"keys": "1"}})
    with pytest.raises(ValueError):
        handle_request({})


def test_request_rejects_unknown_config_key():
    with pytest.raises(ValueError):
        handle_request(
            {"synthesize": {"keys": "1"}, "detector": {"not_a_field": 1}}
        )


def test_cli_synthesize():
    proc = subprocess.run(
        [sys.executable, "-m", "dtmf_service.cli", "--synthesize", "159#", "--snr", "20"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["digits"] == "159#"


def test_cli_request_file(tmp_path):
    req = tmp_path / "req.json"
    req.write_text(json.dumps({"synthesize": {"keys": "0", "seed": 13}}))
    proc = subprocess.run(
        [sys.executable, "-m", "dtmf_service.cli", "--request", str(req)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["digits"] == "0"
