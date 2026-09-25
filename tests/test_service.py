"""Tests for the request-driven offline service."""

import json

import numpy as np
import pytest

from fixed_iir.service import process_request, process_request_file


def _base_request(tmp_path):
    return {
        "filter": {"type": "butter_lowpass", "order": 4, "cutoff": 0.2},
        "fixed": {"sig": [1, 15], "coef": [2, 14], "guard_bits": 2,
                  "rounding": "convergent"},
        "input": {"kind": "sine", "n": 512, "freq_fraction": 0.05,
                  "amplitude": 0.9},
        "output": {"dir": str(tmp_path), "basename": "t",
                   "pcm": True, "csv": True, "npy": True},
    }


def test_process_request_ok_and_files(tmp_path):
    resp = process_request(_base_request(tmp_path))
    assert resp["ok"] is True
    assert resp["input"]["n_samples"] == 512
    assert resp["filter"]["n_sections"] == 2
    files = resp["output"]["files"]
    for key in ("pcm", "csv", "npy", "reference_npy", "fixed_int_npy"):
        assert key in files
    y = np.load(files["npy"])
    ref = np.load(files["reference_npy"])
    assert y.shape == (512,)
    assert np.max(np.abs(y - ref)) < 1e-3
    assert resp["stability"]["verdict"] in ("ok", "warning")
    assert resp["output"]["vs_float_reference"]["max_abs_error"] < 1e-3


def test_process_request_explicit_sos_and_analyze(tmp_path):
    req = _base_request(tmp_path)
    req["filter"] = {"type": "sos",
                     "sections": [[0.25, 0.0, -0.25, 1.0, -1.9, 0.91]]}
    req["fixed"] = {"sig": [1, 5], "coef": [2, 2], "guard_bits": 1,
                    "rounding": "floor"}
    req["input"] = {"kind": "impulse", "n": 128, "amplitude": 1.0}
    req["analyze"] = True
    resp = process_request(req)
    assert resp["ok"] is True
    assert resp["stability"]["verdict"] == "unsafe"
    assert resp["stability"]["max_pole_radius_quantized"] == pytest.approx(1.5)
    assert resp["stability"]["max_pole_radius_float"] < 1.0
    assert "acceptance" in resp
    assert resp["acceptance"]["impulse"]["tail_rms"] > 0.5


def test_process_request_pcm_input(tmp_path):
    x = 0.5 * np.sin(2 * np.pi * 0.03 * np.arange(256))
    from fixed_iir import signals
    src = tmp_path / "in.pcm"
    signals.write_pcm(str(src), x, "int16")
    req = _base_request(tmp_path)
    req["input"] = {"kind": "pcm", "path": str(src), "dtype": "int16"}
    resp = process_request(req)
    assert resp["ok"] is True
    assert resp["input"]["n_samples"] == 256


@pytest.mark.parametrize("kind,extra", [
    ("multi_tone", {"components": [[0.05, 0.3], [0.2, 0.2]]}),
    ("chirp", {"f0": 0.0, "f1": 0.4}),
    ("noise", {"seed": 7}),
])
def test_process_request_input_kinds(tmp_path, kind, extra):
    req = _base_request(tmp_path)
    req["input"] = {"kind": kind, "n": 256, "amplitude": 0.5, **extra}
    resp = process_request(req)
    assert resp["ok"] is True
    assert resp["input"]["n_samples"] == 256


def test_process_request_cheby_filter(tmp_path):
    req = _base_request(tmp_path)
    req["filter"] = {"type": "cheby1_lowpass", "order": 5, "cutoff": 0.15,
                     "ripple_db": 0.5}
    resp = process_request(req)
    assert resp["ok"] is True
    assert resp["filter"]["n_sections"] == 3  # 2 biquads + 1 padded 1st order


def test_process_request_errors_are_reported_not_raised(tmp_path):
    resp = process_request({"filter": {"type": "nope"}})
    assert resp["ok"] is False
    assert "error" in resp
    resp = process_request({"input": {"kind": "pcm", "path": "/no/such/file"}})
    assert resp["ok"] is False
    resp = process_request({"filter": {"type": "sos",
                                       "sections": [[1, 0, 0, 2, 0, 0]]}})
    assert resp["ok"] is False  # a0 != 1


def test_process_request_file_roundtrip(tmp_path):
    req = _base_request(tmp_path)
    req_path = tmp_path / "req.json"
    req_path.write_text(json.dumps(req))
    resp_path = tmp_path / "resp.json"
    resp = process_request_file(str(req_path), str(resp_path))
    assert resp["ok"] is True
    loaded = json.loads(resp_path.read_text())
    assert loaded["ok"] is True
    assert loaded["stability"]["verdict"] == resp["stability"]["verdict"]
