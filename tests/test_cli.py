"""CLI smoke tests."""

import json

import numpy as np
import pytest

from spectral_peak.cli import main

SR = 48000.0
N = 4096
BIN_HZ = SR / N


def test_synth_command_outputs_json(capsys):
    freq = 100.37 * BIN_HZ
    rc = main(["synth", "--freq", str(freq), "--amp", "0.8",
               "--sr", str(SR), "--n", str(N)])
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    top = max(result["peaks"], key=lambda p: p["amplitude"])
    assert abs(top["freq_hz"] - freq) < 0.02 * BIN_HZ


def test_pcm_command_with_real_file(tmp_path):
    freq = 120.23 * BIN_HZ
    t = np.arange(N) / SR
    x = 0.6 * np.sin(2 * np.pi * freq * t)
    path = tmp_path / "tone.pcm"
    (x * 32767).astype("<i2").tofile(path)
    out_file = tmp_path / "result.json"
    rc = main(["pcm", "--file", str(path), "--fmt", "s16le",
               "--sr", str(SR), "--out", str(out_file)])
    assert rc == 0
    result = json.loads(out_file.read_text())
    top = max(result["peaks"], key=lambda p: p["amplitude"])
    assert abs(top["freq_hz"] - freq) < 0.05 * BIN_HZ
    assert top["amplitude"] == pytest.approx(0.6, rel=0.02)
