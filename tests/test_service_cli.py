"""PCM 读写与服务层/CLI 测试。"""

import json

import numpy as np
import pytest

from spectral_peak.pcm import load_pcm, save_pcm_int16
from spectral_peak.service import analyze, analyze_request, synthesize
from spectral_peak.cli import main as cli_main

FS = 48000.0
N = 4096
BIN_HZ = FS / N


def test_pcm_int16_roundtrip(tmp_path):
    freq = 100.37 * BIN_HZ
    samples = 0.8 * synthesize([{"frequency_hz": freq}], FS, N)
    path = tmp_path / "tone.raw"
    save_pcm_int16(str(path), samples)
    loaded = load_pcm(str(path), dtype="int16")
    assert loaded.shape == (N,)
    assert np.max(np.abs(loaded - samples)) < 1.0 / 32767.0 + 1e-12
    peak = analyze(loaded, FS)["peaks"][0]
    assert abs(peak["frequency_hz"] - freq) < 0.01 * BIN_HZ


def test_pcm_offset_and_max_samples(tmp_path):
    path = tmp_path / "ramp.raw"
    save_pcm_int16(str(path), np.linspace(-0.5, 0.5, 1000))
    loaded = load_pcm(str(path), dtype="int16", offset_samples=100, max_samples=200)
    assert loaded.shape == (200,)


def test_pcm_bad_dtype_and_offset(tmp_path):
    path = tmp_path / "x.raw"
    save_pcm_int16(str(path), np.zeros(100))
    with pytest.raises(ValueError):
        load_pcm(str(path), dtype="int8")
    with pytest.raises(ValueError):
        load_pcm(str(path), dtype="int16", offset_samples=1000)


def test_analyze_request_synth_mode():
    request = {
        "mode": "synth",
        "sample_rate": FS,
        "n_samples": N,
        "tones": [{"frequency_hz": 100.37 * BIN_HZ, "amplitude": 1.0}],
        "analysis": {"window": "hann", "method": "auto"},
    }
    result = analyze_request(request)
    assert len(result["peaks"]) == 1
    assert abs(result["peaks"][0]["frequency_hz"] - 100.37 * BIN_HZ) < 0.01 * BIN_HZ


def test_analyze_request_pcm_mode(tmp_path):
    freq = 50.25 * BIN_HZ
    path = tmp_path / "tone.raw"
    save_pcm_int16(str(path), 0.9 * synthesize([{"frequency_hz": freq}], FS, N))
    request = {"mode": "pcm", "path": str(path), "dtype": "int16", "sample_rate": FS}
    result = analyze_request(request)
    assert abs(result["peaks"][0]["frequency_hz"] - freq) < 0.01 * BIN_HZ


def test_analyze_request_validation():
    with pytest.raises(ValueError):
        analyze_request({"mode": "synth", "sample_rate": -1})
    with pytest.raises(ValueError):
        analyze_request({"mode": "bogus", "sample_rate": FS})
    with pytest.raises(ValueError):
        analyze_request({"mode": "pcm", "sample_rate": FS})


def test_cli_synth_stdout(capsys):
    rc = cli_main(["synth", "--freq", "440.25", "--fs", "48000", "--n", "4096"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert abs(out["peaks"][0]["frequency_hz"] - 440.25) < BIN_HZ * 0.01


def test_cli_run_request_file(tmp_path, capsys):
    req = {
        "mode": "synth", "sample_rate": FS, "n_samples": N,
        "tones": [{"frequency_hz": 440.25, "amplitude": 1.0}],
    }
    req_path = tmp_path / "req.json"
    req_path.write_text(json.dumps(req), encoding="utf-8")
    out_path = tmp_path / "result.json"
    rc = cli_main(["run", "--request", str(req_path), "--out", str(out_path)])
    assert rc == 0
    result = json.loads(out_path.read_text(encoding="utf-8"))
    assert abs(result["peaks"][0]["frequency_hz"] - 440.25) < BIN_HZ * 0.01


def test_cli_pcm_and_error_paths(tmp_path, capsys):
    path = tmp_path / "tone.raw"
    save_pcm_int16(str(path), synthesize([{"frequency_hz": 440.25}], FS, N))
    rc = cli_main(["pcm", "--path", str(path), "--dtype", "int16", "--fs", "48000"])
    assert rc == 0
    capsys.readouterr()
    rc = cli_main(["pcm", "--path", str(tmp_path / "missing.raw"),
                   "--dtype", "int16", "--fs", "48000"])
    assert rc == 2
    assert "错误" in capsys.readouterr().err
