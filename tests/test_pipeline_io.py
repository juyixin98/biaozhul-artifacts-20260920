"""文件读取（裸 PCM / WAV）与端到端流水线的测试。"""

import json

import numpy as np
import pytest

from delay_correlator.pipeline import run_analysis
from delay_correlator.signals import (
    SyntheticSpec,
    make_delayed_pair,
    read_raw_pcm,
    read_wav,
    save_raw_pcm,
    write_wav16,
)


def test_raw_pcm_s16_stereo_roundtrip(tmp_path):
    spec = SyntheticSpec(kind="noise", duration=0.25, sample_rate=8000,
                         delay_samples=9, seed=4)
    ref, chan = make_delayed_pair(spec)
    # 白噪声按 RMS 归一化后个别峰值可能超过满量程，写整数 PCM 前限幅以模拟真实前端。
    ref, chan = np.clip(ref, -1, 1), np.clip(chan, -1, 1)
    p = tmp_path / "pair.pcm"
    save_raw_pcm(str(p), ref, chan, dtype="s16", interleaved=True)

    r, sr = read_raw_pcm(str(p), dtype="s16", channels=2, channel_index=0,
                         sample_rate=8000)
    c, _ = read_raw_pcm(str(p), dtype="s16", channels=2, channel_index=1)
    assert sr == 8000
    assert r.size == ref.size and c.size == chan.size
    # 量化往返：编码到最近码点、解码回码点中点，最大误差约 1.5/32767。
    np.testing.assert_allclose(r, ref, atol=5e-5)
    np.testing.assert_allclose(c, chan, atol=5e-5)


def test_raw_pcm_u8_and_f32_readable(tmp_path):
    x = (np.sin(2 * np.pi * 100 * np.arange(800) / 8000) * 0.5).astype(np.float64)
    for dtype in ("u8", "f32", "s32"):
        p = tmp_path / f"x_{dtype}.pcm"
        save_raw_pcm(str(p), x, x, dtype=dtype, interleaved=True)
        r, _ = read_raw_pcm(str(p), dtype=dtype, channels=2, channel_index=0)
        tol = {"u8": 1 / 128 + 1e-9, "f32": 1e-6, "s32": 1e-9}[dtype]
        np.testing.assert_allclose(r, x, atol=tol)


def test_wav16_stereo_roundtrip(tmp_path):
    spec = SyntheticSpec(kind="chirp", duration=0.25, sample_rate=8000,
                         delay_samples=-9, seed=6)
    ref, chan = make_delayed_pair(spec)
    p = tmp_path / "pair.wav"
    write_wav16(str(p), [ref, chan], 8000)

    r, sr = read_wav(str(p), channel_index=0)
    c, _ = read_wav(str(p), channel_index=1)
    assert sr == 8000
    np.testing.assert_allclose(r, ref, atol=4e-5)
    np.testing.assert_allclose(c, chan, atol=4e-5)
    with pytest.raises(ValueError):
        read_wav(str(p), channel_index=2)


def test_pipeline_synthetic_end_to_end(tmp_path):
    request = {
        "source": {
            "mode": "synthetic", "kind": "noise", "duration": 0.5,
            "sample_rate": 8000, "delay_samples": 37, "seed": 11,
        },
        "estimator": {"max_lag": 128, "min_peak": 0.6},
    }
    req_path = tmp_path / "req.json"
    req_path.write_text(json.dumps(request), encoding="utf-8")
    out = tmp_path / "out"

    result = run_analysis(request, base_dir=tmp_path, output_dir=out)
    assert result["summary"]["all_confident"] is True
    assert result["summary"]["delay_samples_mean"] == pytest.approx(37, abs=1e-9)
    assert (out / "results.json").is_file()
    assert (out / "correlation.npz").is_file()
    assert (out / "signals.npz").is_file()

    loaded = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert loaded["windows"][0]["delay_samples"] == pytest.approx(37, abs=1e-9)
    assert loaded["windows"][0]["status"] == "confident"

    npz = np.load(out / "correlation.npz")
    assert npz["lags"].shape == (257,)
    assert npz["ncc"].shape == (1, 257)
    # 存档的 NCC 峰位置同样应为 +37
    assert int(np.argmax(np.abs(npz["ncc"][0]))) - 128 == 37

    # 重复运行拒绝覆盖
    with pytest.raises(FileExistsError):
        run_analysis(request, base_dir=tmp_path, output_dir=out)


def test_pipeline_file_input_pcm(tmp_path):
    spec = SyntheticSpec(kind="noise", duration=0.5, sample_rate=8000,
                         delay_samples=21, seed=15)
    ref, chan = make_delayed_pair(spec)
    save_raw_pcm(str(tmp_path / "pair.pcm"), ref, chan,
                 dtype="s16", interleaved=True)
    request = {
        "source": {
            "mode": "file", "kind": "raw_pcm", "path": "pair.pcm",
            "dtype": "s16", "channels": 2, "ref_channel": 0,
            "channel": 1, "sample_rate": 8000,
        },
        "estimator": {"max_lag": 128},
    }
    result = run_analysis(request, base_dir=tmp_path, output_dir=tmp_path / "out")
    assert result["summary"]["all_confident"] is True
    assert result["summary"]["delay_samples_mean"] == pytest.approx(21, abs=1.5)


def test_pipeline_windowed_silence_and_unknown_param(tmp_path):
    request = {
        "source": {
            "mode": "synthetic", "kind": "noise", "duration": 1.0,
            "sample_rate": 4000, "delay_samples": 5, "seed": 8,
        },
        "estimator": {"max_lag": 64, "window_size": 1000, "min_rms_db": -60.0},
    }
    out = tmp_path / "out"
    result = run_analysis(request, base_dir=tmp_path, output_dir=out)
    assert result["summary"]["n_windows"] == 4
    assert result["summary"]["n_confident"] == 4

    bad = dict(request)
    bad["estimator"] = {"max_lag": 64, "bogus_param": 1}
    with pytest.raises(ValueError, match="未知参数"):
        run_analysis(bad, base_dir=tmp_path, output_dir=tmp_path / "bad")


def test_pipeline_rejects_signal_too_short(tmp_path):
    request = {
        "source": {
            "mode": "synthetic", "kind": "noise", "duration": 0.01,
            "sample_rate": 8000, "delay_samples": 0,
        },
        "estimator": {"max_lag": 128},
    }
    with pytest.raises(ValueError, match="信号长度"):
        run_analysis(request, base_dir=tmp_path, output_dir=tmp_path / "out")
