"""Tests for PCM I/O and the command-line interface."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from sudden_anomaly.detector import DetectorConfig, detect_offline
from sudden_anomaly.pcm_io import (
    make_pcm_fixture,
    read_pcm,
    write_json_summary,
    write_pcm_float64,
    write_point_csv,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestPcmRoundTrip(unittest.TestCase):
    def test_float64_round_trip(self):
        x = np.linspace(-2.0, 2.0, 500)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.f64"
            write_pcm_float64(p, x)
            y, info = read_pcm(p, sample_format="float64", sample_rate=50.0)
            np.testing.assert_allclose(y, x)
            self.assertEqual(info.n_samples, 500)
            self.assertEqual(info.sample_rate, 50.0)

    def test_s16_scaling_and_spike(self):
        x = np.zeros(400)
        x[200] = 0.5
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.s16"
            make_pcm_fixture(p, x)
            y, _ = read_pcm(p, sample_format="s16")
            np.testing.assert_allclose(y, x, atol=1e-4)

    def test_uint8_offset_removed(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.u8"
            # 128 is the zero point; signal is all zeros -> all bytes 128.
            np.full(100, 128, dtype="u1").tofile(p)
            y, _ = read_pcm(p, sample_format="uint8")
            np.testing.assert_allclose(y, 0.0, atol=1e-12)

    def test_unknown_format_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.bin"
            p.write_bytes(b"\x00" * 16)
            with self.assertRaises(ValueError):
                read_pcm(p, sample_format="mp3")

    def test_empty_file_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "empty.s16"
            p.write_bytes(b"")
            with self.assertRaises(ValueError):
                read_pcm(p, sample_format="s16")


class TestOutputFiles(unittest.TestCase):
    def test_csv_and_json_contain_decisions(self):
        cfg = DetectorConfig(window_size=64, prime_size=16, min_history=8)
        x = np.random.default_rng(0).normal(0.0, 1.0, 200)
        x[150] += 15.0
        res = detect_offline(x, cfg)
        with tempfile.TemporaryDirectory() as td:
            csv_path = Path(td) / "points.csv"
            json_path = Path(td) / "summary.json"
            write_point_csv(csv_path, res, sample_rate=100.0, signal=x)
            write_json_summary(json_path, result=res, config={"threshold": 5.0})
            lines = csv_path.read_text().strip().splitlines()
            self.assertEqual(len(lines), 201)  # header + 200 rows
            self.assertTrue(lines[0].startswith("index,time_seconds"))
            payload = json.loads(json_path.read_text())
            self.assertEqual(payload["input"]["n_samples"], 200)
            self.assertIn(150, payload["detection"]["anomaly_indices"])


class TestCli(unittest.TestCase):
    def _run(self, *args):
        env_extra = {"PYTHONPATH": str(REPO_ROOT / "src")}
        import os
        env = dict(os.environ)
        env.update(env_extra)
        return subprocess.run(
            [sys.executable, "-m", "sudden_anomaly.cli", *args],
            capture_output=True, text=True, env=env, check=True,
        )

    def test_synthetic_all_writes_files_and_json(self):
        with tempfile.TemporaryDirectory() as td:
            proc = self._run(
                "--scenario", "all", "--out-dir", td,
                "--block-size", "137", "--json",
            )
            payload = json.loads(proc.stdout[proc.stdout.index("{"):])
            self.assertEqual(len(payload["scenarios"]), 3)
            for stem in ("step", "drift", "spikes"):
                self.assertTrue((Path(td) / f"{stem}_points.csv").exists())
                self.assertTrue((Path(td) / f"{stem}_summary.json").exists())

    def test_pcm_mode(self):
        with tempfile.TemporaryDirectory() as td:
            x = np.zeros(400)
            x[250] = 0.8
            pcm = Path(td) / "sig.s16"
            make_pcm_fixture(pcm, x)
            out = Path(td) / "out"
            proc = self._run(
                "--pcm", str(pcm), "--pcm-format", "s16",
                "--window-size", "64", "--prime-size", "16",
                "--min-history", "8", "--out-dir", str(out),
            )
            self.assertIn("anomaly decisions", proc.stdout)
            self.assertTrue((out / "pcm_points.csv").exists())
            self.assertTrue((out / "pcm_summary.json").exists())

    def test_missing_rate_scenario(self):
        with tempfile.TemporaryDirectory() as td:
            self._run(
                "--scenario", "spikes", "--missing-rate", "0.1",
                "--out-dir", td,
            )
            payload = json.loads(
                (Path(td) / "spikes_missing_summary.json").read_text()
            )
            self.assertGreater(payload["detection"]["missing_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
