"""8. 服务端到端:JSON 请求 -> 数值报告 + 输出文件。"""

import json
import os
import tempfile
import unittest

import numpy as np

import _bootstrap  # noqa: F401
from fir_stream import pcm
from fir_stream.service import run_request


def base_request(out_dir):
    return {
        "sample_rate": 48000,
        "input": {"kind": "synthetic",
                  "signal": {"kind": "mixed", "n": 5000, "channels": 2,
                             "seed": 1}},
        "filter": {"kind": "lowpass", "cutoff": 1000, "num_taps": 65},
        "chunk_size": 512,
        "schedule": [
            {"at": 2000,
             "filter": {"kind": "lowpass", "cutoff": 3000, "num_taps": 65},
             "fade_len": 256},
            {"at": 2100,   # 上一次渐变进行中再次切换
             "filter": {"kind": "highpass", "cutoff": 300, "num_taps": 65},
             "fade_len": 128, "channels": [0]},
        ],
        "output": {"dir": out_dir, "pcm_dtype": "f32le",
                   "write_input_pcm": True},
    }


class TestService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_synthetic_end_to_end(self):
        report = run_request(base_request(self.out))
        # 报告数值
        self.assertEqual(report["n_input_samples"], 5000)
        self.assertEqual(report["n_output_samples"], 5000 + 64)
        self.assertEqual(report["n_switches"], 2)
        self.assertLess(report["reference_max_abs_err"], 1e-9)
        self.assertTrue(report["discontinuity"]["ok"])
        # 输出文件存在且尺寸正确
        out_pcm = report["files"]["output_pcm"]
        self.assertTrue(os.path.exists(out_pcm))
        y = pcm.read_pcm(out_pcm, channels=2, dtype="f32le")
        self.assertEqual(y.shape, (5064, 2))
        self.assertTrue(os.path.exists(report["files"]["input_pcm"]))
        with open(report["files"]["report_json"], encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["n_blocks"], report["n_blocks"])

    def test_pcm_input_roundtrip(self):
        # 先造一个 PCM 输入文件,再以文件为输入跑服务
        src = os.path.join(self.out, "src.pcm")
        rng = np.random.default_rng(8)
        x = 0.5 * rng.standard_normal((3000, 2))
        pcm.write_pcm(src, x, dtype="s16le")
        req = {
            "sample_rate": 48000,
            "input": {"kind": "pcm", "path": src, "channels": 2,
                      "dtype": "s16le"},
            "filter": {"kind": "delay", "k": 5},
            "chunk_size": 256,
            "schedule": [],
            "output": {"dir": os.path.join(self.out, "o2"),
                       "pcm_dtype": "f64le"},
        }
        report = run_request(req)
        self.assertTrue(report["discontinuity"]["ok"])
        self.assertLess(report["reference_max_abs_err"], 1e-9)
        y = pcm.read_pcm(report["files"]["output_pcm"], channels=2,
                         dtype="f64le")
        # delay(5):y[n] = x[n-5],尾部为最后 5 个样本
        xq = pcm.read_pcm(src, channels=2, dtype="s16le")
        np.testing.assert_allclose(y[5:3000], xq[:2995], atol=1e-12)
        np.testing.assert_allclose(y[3000:3005], xq[2995:3000], atol=1e-12)

    def test_chunk_size_changes_nothing(self):
        r1 = run_request(base_request(os.path.join(self.out, "a")))
        req = base_request(os.path.join(self.out, "b"))
        req["chunk_size"] = 333
        r2 = run_request(req)
        ya = pcm.read_pcm(r1["files"]["output_pcm"], channels=2, dtype="f32le")
        yb = pcm.read_pcm(r2["files"]["output_pcm"], channels=2, dtype="f32le")
        self.assertEqual(ya.shape, yb.shape)
        np.testing.assert_allclose(ya, yb, atol=1e-6)  # f32 量化精度内一致

    def test_event_at_sample_zero_and_at_end(self):
        req = base_request(os.path.join(self.out, "c"))
        req["schedule"] = [
            {"at": 0, "filter": {"kind": "identity"}, "fade_len": 0},
            {"at": 5000, "filter": {"kind": "delay", "k": 3}, "fade_len": 0},
        ]
        report = run_request(req)
        self.assertTrue(report["discontinuity"]["ok"])
        self.assertLess(report["reference_max_abs_err"], 1e-9)

    def test_invalid_event_position_raises(self):
        req = base_request(os.path.join(self.out, "d"))
        req["schedule"] = [{"at": 9999, "filter": {"kind": "identity"}}]
        with self.assertRaises(ValueError):
            run_request(req)


if __name__ == "__main__":
    unittest.main()
