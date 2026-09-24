"""服务层与 CLI 端到端测试：合成/PCM 输入、分块、聚合、落盘产物。"""

import json
import os
import subprocess
import sys

import numpy as np
import pytest

from delay_correlator.pcmio import read_pcm, write_pcm, write_wav
from delay_correlator.service import load_request, run_request
from delay_correlator.signals import shift_signal, white_noise

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_cli(*args):
    """以项目根为 PYTHONPATH 运行 CLI，返回 CompletedProcess。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = PROJECT_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "delay_correlator", *args],
        capture_output=True, text=True, env=env,
    )


def test_synthetic_end_to_end_positive(tmp_path):
    req = {
        "input": {
            "type": "synthetic",
            "signal_type": "noise",
            "n": 8000,
            "sample_rate": 8000,
            "delay": 17,
            "snr_db": 30,
            "seed": 42,
        },
        "analysis": {"max_lag_samples": 100},
    }
    res = run_request(req, base_dir=tmp_path)
    assert res["aggregate"]["status"] == "ok"
    assert res["aggregate"]["lag_samples"] == 17
    assert res["aggregate"]["lag_seconds"] == pytest.approx(17 / 8000)
    assert res["n_windows"] == 1
    assert res["windows"][0]["lag_samples"] == 17
    assert res["sign_convention"]  # 非空符号说明


def test_synthetic_negative_delay(tmp_path):
    req = {
        "input": {
            "type": "synthetic",
            "signal_type": "noise",
            "n": 8000,
            "sample_rate": 8000,
            "delay": -23,
            "seed": 5,
        },
        "analysis": {"max_lag_samples": 100},
    }
    res = run_request(req, base_dir=tmp_path)
    assert res["aggregate"]["lag_samples"] == -23
    assert res["aggregate"]["status"] == "ok"


def test_max_lag_in_ms_conversion(tmp_path):
    req = {
        "input": {"type": "synthetic", "signal_type": "noise", "n": 4000,
                  "sample_rate": 1000, "delay": 5, "seed": 1},
        "analysis": {"max_lag_ms": 20},
    }
    res = run_request(req, base_dir=tmp_path)
    assert res["config"]["max_lag_samples"] == 20
    assert res["aggregate"]["lag_samples"] == 5


def test_chunked_windows_consistent(tmp_path):
    # 全程恒定延迟 -> 每个窗口都应报 10，聚合 ok
    req = {
        "input": {"type": "synthetic", "signal_type": "noise", "n": 8000,
                  "sample_rate": 8000, "delay": 10, "seed": 2},
        "analysis": {"max_lag_samples": 50, "window_ms": 250, "hop_ms": 250},
    }
    res = run_request(req, base_dir=tmp_path)
    assert res["n_windows"] == 4
    assert all(w["lag_samples"] == 10 for w in res["windows"])
    assert res["aggregate"]["status"] == "ok"
    assert res["aggregate"]["lag_spread_samples"] == 0


def test_chunked_inconsistent_windows(tmp_path):
    """前后半段延迟不同 -> 聚合判 inconsistent_windows。"""
    rng = np.random.default_rng(3)
    base = rng.normal(size=8000)
    b = np.zeros(8000)
    b[:4000] = shift_signal(base[:4000], 0)
    b[4000:] = shift_signal(base[4000:], 40)
    stereo = np.column_stack([base, b])
    write_pcm(str(tmp_path / "piecewise.s16.pcm"), stereo, dtype="s16")
    req = {
        "input": {"type": "pcm", "mode": "interleaved",
                  "path": "piecewise.s16.pcm", "dtype": "s16",
                  "sample_rate": 8000},
        "analysis": {"max_lag_samples": 100, "window_size_samples": 4000,
                     "hop_samples": 4000, "window_consistency_samples": 1},
    }
    res = run_request(req, base_dir=tmp_path)
    assert res["n_windows"] == 2
    lags = [w["lag_samples"] for w in res["windows"]]
    assert lags == [0, 40]
    assert res["aggregate"]["status"] == "uncertain"
    assert "inconsistent_windows" in res["aggregate"]["uncertainty_reasons"]
    assert res["aggregate"]["lag_spread_samples"] == 40


def test_pcm_paired_files(tmp_path):
    rng = np.random.default_rng(4)
    a = rng.normal(size=3000)
    b = shift_signal(a, 12)
    write_pcm(str(tmp_path / "a.f32.pcm"), a, dtype="f32")
    write_pcm(str(tmp_path / "b.f32.pcm"), b, dtype="f32")
    req = {
        "input": {"type": "pcm", "mode": "paired", "path_a": "a.f32.pcm",
                  "path_b": "b.f32.pcm", "dtype": "f32", "sample_rate": 8000},
        "analysis": {"max_lag_samples": 50},
    }
    res = run_request(req, base_dir=tmp_path)
    assert res["aggregate"]["lag_samples"] == 12
    assert res["aggregate"]["status"] == "ok"


def test_wav_input(tmp_path):
    rng = np.random.default_rng(6)
    a = rng.normal(size=4000)
    b = shift_signal(a, -8)
    write_wav(str(tmp_path / "in.wav"), np.column_stack([a, b]), 16000)
    req = {
        "input": {"type": "pcm", "mode": "wav", "path": "in.wav"},
        "analysis": {"max_lag_samples": 50},
    }
    res = run_request(req, base_dir=tmp_path)
    assert res["signal"]["sample_rate"] == 16000
    assert res["aggregate"]["lag_samples"] == -8
    assert res["aggregate"]["status"] == "ok"


def test_silence_request_uncertain(tmp_path):
    req = {
        "input": {"type": "synthetic", "signal_type": "silence", "n": 2000,
                  "sample_rate": 8000, "delay": 3},
        "analysis": {"max_lag_samples": 30},
    }
    res = run_request(req, base_dir=tmp_path)
    assert res["aggregate"]["status"] == "uncertain"
    assert res["aggregate"]["lag_samples"] is None


def test_periodic_request_uncertain(tmp_path):
    req = {
        "input": {"type": "synthetic", "signal_type": "sine", "n": 4000,
                  "sample_rate": 8000, "freq": 200, "delay": 20},
        "analysis": {"max_lag_samples": 100, "ambiguity_guard_samples": 3},
    }
    res = run_request(req, base_dir=tmp_path)
    assert res["aggregate"]["status"] == "uncertain"
    assert "ambiguous_peaks" in res["windows"][0]["uncertainty_reasons"]


def test_outputs_npz_and_csv(tmp_path):
    req = {
        "input": {"type": "synthetic", "signal_type": "noise", "n": 4000,
                  "sample_rate": 8000, "delay": 6, "seed": 9},
        "analysis": {"max_lag_samples": 40, "window_ms": 250},
        "output": {"profile_npz": "out/profiles.npz", "windows_csv": "out/windows.csv"},
    }
    res = run_request(req, base_dir=tmp_path)
    assert (tmp_path / "out/profiles.npz").exists()
    assert (tmp_path / "out/windows.csv").exists()
    data = np.load(tmp_path / "out/profiles.npz", allow_pickle=True)
    corr = data["corr"]
    lags = data["lags"]
    assert corr.shape[0] == res["n_windows"]
    # 每个剖面的峰滞后都应是 6
    for i in range(corr.shape[0]):
        assert int(lags[i][np.argmax(corr[i])]) == 6
    text = (tmp_path / "out/windows.csv").read_text(encoding="utf-8")
    assert "lag_samples" in text.splitlines()[0]


def test_synthetic_save_wav_then_reanalyze(tmp_path):
    """合成后落盘 WAV，再作为 PCM 输入重分析，结果应一致。"""
    req1 = {
        "input": {"type": "synthetic", "signal_type": "noise", "n": 8000,
                  "sample_rate": 8000, "delay": 15, "seed": 21,
                  "save_wav": "gen.wav"},
        "analysis": {"max_lag_samples": 80},
    }
    res1 = run_request(req1, base_dir=tmp_path)
    req2 = {
        "input": {"type": "pcm", "mode": "wav", "path": "gen.wav"},
        "analysis": {"max_lag_samples": 80},
    }
    res2 = run_request(req2, base_dir=tmp_path)
    assert res2["aggregate"]["lag_samples"] == res1["aggregate"]["lag_samples"] == 15


def test_cli_run_with_request_file(tmp_path):
    req = {
        "input": {"type": "synthetic", "signal_type": "noise", "n": 3000,
                  "sample_rate": 8000, "delay": 9, "seed": 31},
        "analysis": {"max_lag_samples": 50},
    }
    req_path = tmp_path / "req.json"
    out_path = tmp_path / "result.json"
    req_path.write_text(json.dumps(req), encoding="utf-8")
    proc = _run_cli("run", str(req_path), "-o", str(out_path))
    assert proc.returncode == 0, proc.stderr
    printed = json.loads(proc.stdout)
    saved = json.loads(out_path.read_text(encoding="utf-8"))
    assert printed["aggregate"]["lag_samples"] == 9
    assert saved["aggregate"]["lag_samples"] == 9


@pytest.mark.parametrize("case", ["positive-delay", "negative-delay", "silence",
                                  "periodic"])
def test_cli_demo_cases(case):
    proc = _run_cli("demo", "--case", case)
    assert proc.returncode == 0, proc.stderr
    res = json.loads(proc.stdout)
    assert "aggregate" in res
    if case == "positive-delay":
        assert res["aggregate"]["lag_samples"] == 17
    if case == "negative-delay":
        assert res["aggregate"]["lag_samples"] == -17
    if case == "silence":
        assert res["aggregate"]["status"] == "uncertain"
    if case == "periodic":
        assert res["aggregate"]["status"] == "uncertain"
