"""服务层与 CLI 测试：端到端请求执行、文件产物、退出码。"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from pcm_container import wavio
from pcm_container.errors import PcmError
from pcm_container.service import run_request


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def p(self, name):
        return str(self.dir / name)


class TestSynthesizeService(TempCase):
    def test_synthesize_writes_wav_and_stats(self):
        req = {
            "action": "synthesize",
            "wave_type": "sine",
            "sample_rate": 8000,
            "duration": 0.5,
            "frequency": 440,
            "amplitude": 0.5,
            "bits": 16,
            "output_wav": self.p("out.wav"),
        }
        res = run_request(req)
        self.assertEqual(res["n_frames"], 4000)
        self.assertTrue(Path(self.p("out.wav")).is_file())
        parsed = wavio.read_wav(self.p("out.wav"))
        self.assertEqual(parsed.sample_rate, 8000)
        self.assertLessEqual(abs(parsed.samples).max(), int(32768 * 0.5) + 1)
        self.assertGreater(res["stats_float"]["rms"], 0.3)
        self.assertEqual(res["clipped_samples"], 0)

    def test_synthesize_clipping_counted(self):
        req = {
            "action": "synthesize",
            "wave_type": "sine",
            "sample_rate": 1000,
            "duration": 1.0,
            "frequency": 1.0,
            "amplitude": 2.0,
            "bits": 16,
        }
        res = run_request(req)
        self.assertGreater(res["clipped_samples"], 0)

    def test_synthesize_24bit_stereo_npy(self):
        req = {
            "action": "synthesize",
            "wave_type": "noise",
            "seed": 1,
            "sample_rate": 48000,
            "duration": 0.01,
            "channels": 2,
            "bits": 24,
            "output_wav": self.p("s24.wav"),
            "output_npy": self.p("s.npy"),
        }
        res = run_request(req)
        parsed = wavio.read_wav(self.p("s24.wav"))
        self.assertEqual(parsed.bits, 24)
        self.assertEqual(parsed.samples.shape, (480, 2))
        arr = np.load(self.p("s.npy"))
        self.assertEqual(arr.shape, (480, 2))


class TestInspectAndConvert(TempCase):
    def _make(self, bits, samples, **kw):
        path = self.p(f"in{bits}.wav")
        wavio.write_wav(path, np.asarray(samples, dtype=np.int32), 48000, 1, bits, **kw)
        return path

    def test_inspect_report_fields(self):
        path = self._make(24, [-8388608, 0, 8388607])
        res = run_request({"action": "inspect_wav", "input_wav": path})
        self.assertEqual(res["container"]["bits"], 24)
        self.assertEqual(res["container"]["n_frames"], 3)
        self.assertTrue(res["container"]["riff_size_matches_file"])
        self.assertEqual(res["stats_float"]["min"], -1.0)
        self.assertEqual(res["stats_float"]["max"], 8388607 / 8388608)

    def test_downconvert_24_to_16_counts_domain_loss(self):
        path = self._make(24, [-8388608, 0, 8388607])
        out = self.p("down.wav")
        res = run_request(
            {
                "action": "convert",
                "input_wav": path,
                "output_wav": out,
                "output_bits": 16,
            }
        )
        self.assertEqual(res["samples_out_of_target_domain"], 2)
        parsed = wavio.read_wav(out)
        self.assertEqual(parsed.samples.reshape(-1).tolist(), [-32768, 0, 32767])

    def test_convert_16_to_24_roundtrip_amplitude(self):
        codes = np.linspace(-1000, 1000, 21).astype(np.int32)
        path = self._make(16, codes)
        out = self.p("up.wav")
        run_request(
            {"action": "convert", "input_wav": path, "output_wav": out, "output_bits": 24}
        )
        parsed = wavio.read_wav(out)
        # 升位深：码值按比例平移，振幅完全一致
        np.testing.assert_allclose(
            parsed.samples.reshape(-1).astype(np.float64) / 2**23,
            codes.astype(np.float64) / 2**15,
        )

    def test_resampling_rejected(self):
        path = self._make(16, [1, 2, 3])
        with self.assertRaises(PcmError):
            run_request(
                {
                    "action": "convert",
                    "input_wav": path,
                    "output_wav": self.p("x.wav"),
                    "output_sample_rate": 24000,
                }
            )

    def test_wav_to_raw_and_back(self):
        codes = np.array([-100, 0, 100, 32767], dtype=np.int32)
        path = self._make(16, codes)
        raw_path = self.p("out.raw")
        run_request(
            {"action": "wav_to_raw", "input_wav": path, "output_raw": raw_path}
        )
        raw = Path(raw_path).read_bytes()
        self.assertEqual(len(raw), 8)
        back = self.p("back.wav")
        run_request(
            {
                "action": "raw_to_wav",
                "input_raw": raw_path,
                "output_wav": back,
                "sample_rate": 48000,
                "channels": 1,
                "bits": 16,
            }
        )
        parsed = wavio.read_wav(back)
        np.testing.assert_array_equal(parsed.samples.reshape(-1), codes)

    def test_raw_alignment_error(self):
        raw_path = self.p("bad.raw")
        Path(raw_path).write_bytes(b"\x00" * 7)  # 24 位单声道：7 % 3 != 0
        with self.assertRaises(Exception):
            run_request(
                {
                    "action": "raw_to_wav",
                    "input_raw": raw_path,
                    "output_wav": self.p("b.wav"),
                    "bits": 24,
                }
            )


class TestCli(TempCase):
    def test_cli_success_exit0(self):
        req = self.p("req.json")
        Path(req).write_text(
            json.dumps(
                {
                    "action": "synthesize",
                    "wave_type": "sine",
                    "sample_rate": 1000,
                    "duration": 1.0,
                    "frequency": 1.0,
                    "output_wav": self.p("o.wav"),
                }
            )
        )
        proc = subprocess.run(
            [sys.executable, "-m", "pcm_container", "run", req],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["action"], "synthesize")

    def test_cli_rejected_format_exit2(self):
        # 合法容器、8 位 PCM -> 必须被格式拒绝（退出码 2，WavFormatError）
        import struct

        def chk(cid, body):
            pad = b"\x00" if len(body) & 1 else b""
            return cid + struct.pack("<I", len(body)) + body + pad

        fmt = struct.pack("<HHIIHH", 1, 1, 8000, 8000, 1, 8)
        chunks = chk(b"fmt ", fmt) + chk(b"data", b"\x80" * 8)
        bad = b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WAVE" + chunks
        wav_path = self.p("eight.wav")
        Path(wav_path).write_bytes(bad)
        req = self.p("req.json")
        Path(req).write_text(
            json.dumps({"action": "inspect_wav", "input_wav": wav_path})
        )
        proc = subprocess.run(
            [sys.executable, "-m", "pcm_container", "run", req],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2)
        err = json.loads(proc.stderr)
        self.assertEqual(err["error_type"], "WavFormatError")

    def test_cli_missing_file_exit1(self):
        proc = subprocess.run(
            [sys.executable, "-m", "pcm_container", "run", self.p("nope.json")],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 1)


if __name__ == "__main__":
    unittest.main()
