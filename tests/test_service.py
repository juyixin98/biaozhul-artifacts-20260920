"""pcm.service:离线请求处理(校验/合成/往返)测试。"""

from __future__ import annotations

import json
import os
import struct
import tempfile
import unittest

import numpy as np

from pcmval import pcm, service, synth, wavio
from pcmval.wavio import WavFormatError

from .helpers import build_wav, fmt_payload, pcm_frames, raw_chunk


class TestValidateWav(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _path(self, name):
        return os.path.join(self.dir, name)

    def test_ok_file_with_junk(self):
        blob = build_wav([
            raw_chunk(b"JUNK", b"\xAB" * 5),  # 奇数长度
            raw_chunk(b"fmt ", fmt_payload(bits=24, channels=2, sample_rate=48000)),
            raw_chunk(b"data", pcm_frames(-8388608, 8388607, bits=24)),
        ])
        path = self._path("junk24.wav")
        with open(path, "wb") as fh:
            fh.write(blob)
        res = service.handle({"action": "validate", "path": path})
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["bits_per_sample"], 24)
        self.assertEqual(res["channels"], 2)
        self.assertEqual(res["frames"], 1)
        self.assertEqual(res["sample_stats"]["int_min"], -8388608)
        self.assertEqual(res["sample_stats"]["int_max"], 8388607)
        self.assertEqual(res["sample_stats"]["extreme_negative_count"], 1)
        self.assertEqual(res["sample_stats"]["extreme_positive_count"], 1)
        self.assertTrue(any(c["id"] == "JUNK" and c["handled"] == "skipped"
                            for c in res["chunks"]))
        # JSON 可序列化
        json.dumps(res)

    def test_bad_container_raises(self):
        path = self._path("bad.wav")
        with open(path, "wb") as fh:
            fh.write(b"RF64 not wav at all")
        with self.assertRaises(WavFormatError):
            service.handle({"action": "validate", "path": path})

    def test_raw_pcm_validate_16_and_24(self):
        p16 = self._path("a.pcm")
        with open(p16, "wb") as fh:
            fh.write(pcm_frames(-32768, 32767))
        res = service.handle({"action": "validate", "container": "raw",
                              "path": p16, "bits": 16})
        self.assertEqual(res["container"], "raw-pcm")
        self.assertEqual(res["frames"], 2)
        self.assertEqual(res["sample_stats"]["amplitude_min"], -1.0)

        p24 = self._path("a24.pcm")
        with open(p24, "wb") as fh:
            fh.write(pcm_frames(0, -1, 8388607, bits=24) + b"\x00")  # 10 字节,非 3 倍数
        res = service.handle({"action": "validate", "container": "raw",
                              "path": p24, "bits": 24})
        self.assertEqual(res["frames"], 3)
        self.assertTrue(any(i["code"] == "PARTIAL_FRAME" for i in res["issues"]))

    def test_raw_rejects_bad_bits(self):
        p = self._path("a.pcm")
        open(p, "wb").close()
        with self.assertRaises(service.BadRequestError):
            service.handle({"action": "validate", "container": "raw",
                            "path": p, "bits": 32})


class TestSynthesize(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_synthesize_fullscale_24_writes_wav_and_raw(self):
        wavp = os.path.join(self.dir, "fs24.wav")
        rawp = os.path.join(self.dir, "fs24.pcm")
        res = service.handle({
            "action": "synthesize", "kind": "fullscale",
            "sample_rate": 1000, "duration": 0.008, "bits": 24,
            "out": wavp, "raw_out": rawp,
        })
        self.assertEqual(res["frames"], 8)
        self.assertEqual(res["sample_stats"]["int_min"], -8388608)
        self.assertEqual(res["sample_stats"]["int_max"], 8388607)
        self.assertTrue(os.path.exists(wavp) and os.path.exists(rawp))
        self.assertEqual(os.path.getsize(rawp), 8 * 3)
        # 读回验证
        r = wavio.read_wav(wavp)
        np.testing.assert_array_equal(r.samples[::2], [-8388608] * 4)
        np.testing.assert_array_equal(r.samples[1::2], [8388607] * 4)

    def test_synthesize_stereo_sine_16(self):
        wavp = os.path.join(self.dir, "sine.wav")
        res = service.handle({
            "action": "synthesize", "kind": "sine",
            "sample_rate": 8000, "duration": 0.01, "bits": 16,
            "channels": 2, "out": wavp,
        })
        self.assertEqual(res["channels"], 2)
        r = wavio.read_wav(wavp)
        self.assertEqual(r.samples.shape, (80, 2))
        np.testing.assert_array_equal(r.samples[:, 0], r.samples[:, 1])

    def test_synthesize_rejects_8bit(self):
        with self.assertRaises(service.BadRequestError):
            service.handle({"action": "synthesize", "bits": 8})


class TestRoundtrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_synthetic_fullscale_16(self):
        res = service.handle({
            "action": "roundtrip", "kind": "fullscale",
            "sample_rate": 1000, "duration": 0.016, "bits": 16,
        })
        checks = res["checks"]
        self.assertTrue(checks["payload_bytes_identical"])
        self.assertTrue(checks["integer_samples_identical"])
        self.assertTrue(checks["amplitude_roundtrip_identical"])
        # fullscale 源恰好在整数格点上,量化误差为 0
        self.assertEqual(res["quantization"]["max_abs_error_amplitude"], 0.0)
        self.assertEqual(res["frames"], 16)

    def test_synthetic_sine_24_error_bounded_by_half_lsb(self):
        res = service.handle({
            "action": "roundtrip", "kind": "sine",
            "sample_rate": 48000, "duration": 0.005, "bits": 24,
            "amplitude": 0.9,
        })
        self.assertTrue(res["checks"]["payload_bytes_identical"])
        err = res["quantization"]["max_abs_error_amplitude"]
        self.assertLessEqual(err, res["quantization"]["half_lsb_amplitude"] + 1e-18)

    def test_existing_wav_with_odd_pad_roundtrips_byte_identical(self):
        # 含奇数长度未知 chunk 的文件:重编码(去掉 JUNK)不会字节一致,
        # 但 data 负载与振幅必须一致;干净文件(标准库 wave 产出)整文件一致。
        import io
        import wave
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(struct.pack("<4h", -32768, 0, 32767, 1234))
        path = os.path.join(self.dir, "clean.wav")
        with open(path, "wb") as fh:
            fh.write(buf.getvalue())
        res = service.handle({"action": "roundtrip", "path": path})
        self.assertTrue(res["checks"]["whole_file_rebuild_identical"])
        self.assertTrue(res["checks"]["payload_bytes_identical"])

    def test_unknown_action(self):
        with self.assertRaises(service.BadRequestError):
            service.handle({"action": "explode"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
