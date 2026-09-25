"""End-to-end service job tests and raw-PCM round trips."""

import json
import os
import tempfile

import numpy as np
import unittest

from stft_service.pcm_io import read_pcm, write_pcm
from stft_service.service import error_metrics, run_job
from stft_service.signals import synthesize


class PcmIOTests(unittest.TestCase):
    def test_s16_roundtrip(self):
        x = synthesize(1000)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.pcm")
            n = write_pcm(path, x, "s16")
            self.assertEqual(n, 1000)
            self.assertEqual(os.path.getsize(path), 2000)
            y = read_pcm(path, "s16")
        self.assertLess(np.max(np.abs(y - x)), 1.1 / 32768)

    def test_s32_roundtrip(self):
        x = synthesize(100)
        with tempfile.TemporaryDirectory() as d:
            write_pcm(os.path.join(d, "x.pcm"), x, "s32")
            y = read_pcm(os.path.join(d, "x.pcm"), "s32")
        self.assertLess(np.max(np.abs(y - x)), 2.0 / 2**31)

    def test_float_formats_exact(self):
        x = synthesize(100, noise_std=0.0)
        for fmt in ("f32", "f64"):
            with tempfile.TemporaryDirectory() as d:
                write_pcm(os.path.join(d, "x.pcm"), x, fmt)
                y = read_pcm(os.path.join(d, "x.pcm"), fmt)
            tol = 1e-6 if fmt == "f32" else 1e-14
            self.assertLess(np.max(np.abs(y - x)), tol)

    def test_clipping(self):
        x = np.array([2.0, -2.0, 0.0])
        with tempfile.TemporaryDirectory() as d:
            write_pcm(os.path.join(d, "x.pcm"), x, "s16")
            y = read_pcm(os.path.join(d, "x.pcm"), "s16")
        self.assertLessEqual(np.max(np.abs(y)), 1.0)

    def test_bad_format(self):
        with self.assertRaises(ValueError):
            read_pcm("nope", "mp3")


class MetricsTests(unittest.TestCase):
    def test_perfect_reconstruction(self):
        x = np.zeros(10)
        m = error_metrics(x, x)
        self.assertEqual(m["rmse"], 0.0)
        self.assertEqual(m["max_abs_error"], 0.0)

    def test_known_error(self):
        x = np.ones(4)
        y = x + 0.5
        m = error_metrics(x, y)
        self.assertAlmostEqual(m["rmse"], 0.5)
        self.assertAlmostEqual(m["max_abs_error"], 0.5)
        self.assertAlmostEqual(m["snr_db"], 10 * np.log10(4.0 / 1.0))

    def test_zero_signal_zero_error_snr_is_none(self):
        m = error_metrics(np.zeros(3), np.zeros(3))
        self.assertIsNone(m["snr_db"])

    def test_shape_mismatch(self):
        with self.assertRaises(ValueError):
            error_metrics(np.zeros(3), np.zeros(4))

    def test_empty(self):
        m = error_metrics(np.zeros(0), np.zeros(0))
        self.assertEqual(m["n_samples"], 0)


class ServiceJobTests(unittest.TestCase):
    def _tmpdir(self):
        d = tempfile.mkdtemp()
        self.addCleanup(self._cleanup, d)
        return d

    @staticmethod
    def _cleanup(d):
        import shutil

        shutil.rmtree(d, ignore_errors=True)

    def test_synthetic_job_whole_equals_stream(self):
        d = self._tmpdir()
        req = {
            "input": {"type": "synthetic", "length": 2000, "noise_std": 0.01},
            "stft": {"n_fft": 256, "hop_length": 128},
            "chunk_size": [100, 33],
            "output_dir": "out",
        }
        report = run_job(req, workdir=d)
        self.assertEqual(report["status"], "ok")
        self.assertTrue(report["frames"]["match"])
        self.assertTrue(report["reconstruction"]["possible"])
        met = report["metrics"]
        self.assertLess(met["whole_vs_original"]["max_abs_error"], 1e-10)
        self.assertLess(met["stream_vs_original"]["max_abs_error"], 1e-10)
        self.assertLess(
            met["forward_whole_vs_stream_max_abs"], 1e-12
        )
        self.assertLess(
            met["inverse_whole_vs_stream_max_abs"], 1e-12
        )
        self.assertLess(
            met["inverse_whole_vs_singleframe_stream_max_abs"], 1e-12
        )
        # files exist
        for name, path in report["files"].items():
            self.assertTrue(os.path.exists(path), name)
        # report JSON written
        with open(os.path.join(d, "out", "report.json")) as fh:
            on_disk = json.load(fh)
        self.assertEqual(on_disk["frames"]["whole"],
                         report["frames"]["whole"])

    def test_pcm_job(self):
        d = self._tmpdir()
        x = synthesize(1200)
        write_pcm(os.path.join(d, "sig.pcm"), x, "s16")
        req = {
            "input": {"type": "pcm", "path": "sig.pcm",
                      "sample_format": "s16"},
            "stft": {"n_fft": 128, "hop_length": 64},
            "chunk_size": 200,
            "output_dir": "out",
            "output_format": "s16",
            "write_csv": False,
        }
        report = run_job(req, workdir=d)
        self.assertEqual(report["input"]["length"], 1200)
        self.assertTrue(report["reconstruction"]["possible"])
        # integer quantisation bounds the achievable accuracy
        self.assertLess(
            report["metrics"]["whole_vs_original"]["max_abs_error"],
            1.1 / 32768,
        )
        recon_q = read_pcm(
            os.path.join(d, "out", "reconstructed_whole.s16.pcm"), "s16"
        )
        self.assertEqual(recon_q.shape, x.shape)
        self.assertLess(np.max(np.abs(recon_q - x)), 2.0 / 32768)

    def test_short_inputs_job(self):
        d = self._tmpdir()
        for n in (0, 1, 5, 100):
            req = {
                "input": {"type": "synthetic", "length": n,
                          "noise_std": 0.0},
                "stft": {"n_fft": 64, "hop_length": 32},
                "chunk_size": 7,
                "output_dir": f"out_{n}",
                "write_csv": False,
            }
            report = run_job(req, workdir=d)
            self.assertTrue(report["frames"]["match"])
            self.assertTrue(
                report["reconstruction"]["possible"],
                f"n={n} zero positions: "
                f"{report['reconstruction']['zero_weight_positions']}",
            )
            self.assertLess(
                report["metrics"]["whole_vs_original"]["max_abs_error"],
                1e-9 if n else 1e-12,
            )

    def test_diagnostic_job_flags_zero_weights(self):
        d = self._tmpdir()
        req = {
            "input": {"type": "synthetic", "length": 512,
                      "frequencies": [440.0], "amplitudes": [0.8],
                      "noise_std": 0.0},
            "stft": {"n_fft": 64, "hop_length": 64, "window": "hann"},
            "chunk_size": 64,
            "output_dir": "out",
            "write_csv": False,
        }
        report = run_job(req, workdir=d)
        # run still succeeds as a computation; diagnosis is explicit
        self.assertEqual(report["status"], "ok")
        self.assertFalse(report["reconstruction"]["possible"])
        self.assertFalse(report["diagnostic"]["nola_holds"])
        zeros = report["reconstruction"]["zero_weight_positions"]
        self.assertIn(32, zeros)
        # whole and stream agree even in the non-reconstructable case
        self.assertLess(
            report["metrics"]["inverse_whole_vs_stream_max_abs"], 1e-12
        )

    def test_bad_request_raises(self):
        d = self._tmpdir()
        req = {
            "input": {"type": "synthetic", "length": 100},
            "stft": {"n_fft": 16, "hop_length": 17},
            "output_dir": "out",
        }
        with self.assertRaises(Exception):
            run_job(req, workdir=d)


if __name__ == "__main__":
    unittest.main()
