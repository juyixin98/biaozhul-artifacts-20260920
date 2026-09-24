"""signals 模块与 service CLI 入口的补充测试。"""

import json

import numpy as np
import pytest

from fixedpoint_iir.service import main
from fixedpoint_iir.signals import (
    build_signal,
    gen_chirp,
    gen_multi_sine,
    gen_noise,
    gen_step,
    read_pcm,
    write_pcm,
)


class TestSignalGenerators:
    def test_step(self):
        x = gen_step(8, amplitude=2.0, index=3)
        assert x[2] == 0.0 and x[3] == 2.0 and x[-1] == 2.0

    def test_multi_sine_default_amplitudes(self):
        x = gen_multi_sine(256, [100.0, 200.0], 1000.0)
        assert np.max(np.abs(x)) <= 1.0 + 1e-9

    def test_noise_deterministic(self):
        a = gen_noise(64, seed=42)
        b = gen_noise(64, seed=42)
        np.testing.assert_array_equal(a, b)
        assert np.max(np.abs(a)) <= 1.0

    def test_chirp_bounded(self):
        x = gen_chirp(256, 100.0, 400.0, 1000.0, amplitude=0.8)
        assert np.max(np.abs(x)) <= 0.8 + 1e-9

    def test_build_signal_all_types(self):
        assert build_signal({"type": "step", "n": 8}).shape == (8,)
        assert build_signal({"type": "multi_sine", "n": 8, "freqs_hz": [1.0],
                             "sample_rate": 10.0}).shape == (8,)
        assert build_signal({"type": "chirp", "n": 8, "f0_hz": 1.0, "f1_hz": 2.0,
                             "sample_rate": 10.0}).shape == (8,)
        with pytest.raises(ValueError):
            build_signal({"type": "unknown", "n": 8})


class TestPcmIo:
    def test_roundtrip_s16le(self, tmp_path):
        x = np.linspace(-0.9, 0.9, 100)
        p = tmp_path / "a.pcm"
        write_pcm(str(p), x, "s16le")
        back = read_pcm(str(p), "s16le")
        assert np.max(np.abs(back - x)) < 1e-4

    def test_roundtrip_u8_and_s32le(self, tmp_path):
        x = np.linspace(-0.5, 0.5, 50)
        for fmt, tol in (("u8", 1e-2), ("s32le", 1e-8)):
            p = tmp_path / f"a_{fmt}.pcm"
            write_pcm(str(p), x, fmt)
            assert np.max(np.abs(read_pcm(str(p), fmt) - x)) < tol

    def test_unsupported_format_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            read_pcm(str(tmp_path / "x.pcm"), "f32")
        with pytest.raises(ValueError):
            write_pcm(str(tmp_path / "x.pcm"), np.zeros(4), "f32")

    def test_write_clips_out_of_range(self, tmp_path):
        p = tmp_path / "c.pcm"
        write_pcm(str(p), np.array([2.0, -2.0]), "s16le")
        back = read_pcm(str(p), "s16le")
        assert np.max(np.abs(back)) <= 1.0


class TestServiceCli:
    def test_main_runs_and_prints_summary(self, tmp_path, capsys):
        req = {
            "name": "cli_test",
            "signal": {"type": "impulse", "n": 64, "amplitude": 0.5},
            "sos": [[0.5, 0.0, 0.0, 1.0, -0.5, 0.0]],
        }
        req_path = tmp_path / "req.json"
        req_path.write_text(json.dumps(req), encoding="utf-8")
        rc = main([str(req_path), "--outdir", str(tmp_path / "out")])
        assert rc == 0
        out = capsys.readouterr().out
        assert "cli_test" in out
        assert (tmp_path / "out" / "report.json").exists()
