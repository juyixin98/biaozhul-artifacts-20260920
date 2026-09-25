"""7. 边界不连续检测 + PCM 读写。"""

import unittest

import numpy as np

import _bootstrap  # noqa: F401
from fir_stream.discontinuity import detect_boundary_jumps, max_abs_diff_at
from fir_stream.engine import StreamingFIREngine
from fir_stream import pcm, signals, filters


class TestDiscontinuityDetection(unittest.TestCase):
    def test_clean_detector_finds_inserted_jump(self):
        rng = np.random.default_rng(1)
        y = 0.01 * rng.standard_normal(1000)
        y[500:] += 0.5  # 在边界 500 处制造阶跃
        rep = detect_boundary_jumps(y, [250, 500, 750], threshold=0.1)
        self.assertFalse(rep.ok)
        self.assertEqual(rep.detected_boundaries, [500])
        self.assertAlmostEqual(rep.detected_values[0], 0.5, delta=0.05)

    def test_detector_clean_on_smooth_signal(self):
        t = np.arange(4000) / 48000.0
        y = np.sin(2 * np.pi * 100 * t)
        rep = detect_boundary_jumps(y, [1000, 2000, 3000])
        self.assertTrue(rep.ok, f"误报: {rep.detected_boundaries}")

    def test_detector_auto_threshold(self):
        rng = np.random.default_rng(2)
        y = 0.01 * rng.standard_normal(2000)
        y[1000:] += 0.5
        rep = detect_boundary_jumps(y, [1000])  # 自动阈值
        self.assertFalse(rep.ok)
        self.assertEqual(rep.detected_boundaries, [1000])
        # 自动阈值对干净信号不应误报
        t = np.arange(2000) / 48000.0
        clean = np.sin(2 * np.pi * 100 * t)
        rep2 = detect_boundary_jumps(clean, [500, 1000, 1500])
        self.assertTrue(rep2.ok)

    def test_multichannel_detection(self):
        rng = np.random.default_rng(3)
        y = 0.01 * rng.standard_normal((1000, 2))
        y[400:, 1] += 1.0  # 只有通道 1 在边界 400 处跳变
        rep = detect_boundary_jumps(y, [400], threshold=0.1)
        self.assertFalse(rep.ok)

    def test_engine_output_has_no_boundary_jumps(self):
        # 端到端:带切换的多块处理,所有块边界都必须平滑
        sr = 48000
        t = np.arange(6000) / sr
        x = 0.5 * np.sin(2 * np.pi * 500 * t)
        eng = StreamingFIREngine(filters.lowpass(1000, 65, sr))
        boundaries, outs, pos = [], [], 0
        switches = {2000: filters.lowpass(3000, 65, sr),
                    4000: filters.highpass(200, 65, sr)}
        for size in [512] * 11 + [388]:
            if pos in switches:
                eng.switch(switches[pos], fade_len=256)
            outs.append(eng.process(x[pos:pos + size]))
            pos += size
            boundaries.append(pos)
        outs.append(eng.drain().ravel())
        y = np.concatenate(outs)
        rep = detect_boundary_jumps(y, boundaries[:-1])
        self.assertTrue(rep.ok,
                        f"边界突跳: {list(zip(rep.detected_boundaries, rep.detected_values))}")

    def test_max_abs_diff_at(self):
        y = np.array([0.0, 1.0, 1.5, 1.5, 3.0])
        self.assertAlmostEqual(max_abs_diff_at(y, [2, 4]), 1.5)


class TestPCM(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.path = lambda name: f"{self.tmp.name}/{name}"

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_s16le(self):
        rng = np.random.default_rng(4)
        x = rng.uniform(-0.9, 0.9, (1000, 2))
        pcm.write_pcm(self.path("a.pcm"), x, dtype="s16le")
        y = pcm.read_pcm(self.path("a.pcm"), channels=2, dtype="s16le")
        self.assertEqual(y.shape, (1000, 2))
        np.testing.assert_allclose(y, x, atol=1.0 / 32768.0 + 1e-12)

    def test_roundtrip_f32le_exact(self):
        rng = np.random.default_rng(5)
        x = rng.standard_normal(500).astype(np.float32).astype(np.float64)
        pcm.write_pcm(self.path("b.pcm"), x, dtype="f32le")
        y = pcm.read_pcm(self.path("b.pcm"), channels=1, dtype="f32le")
        np.testing.assert_array_equal(y, x)

    def test_roundtrip_s24le(self):
        rng = np.random.default_rng(6)
        x = rng.uniform(-0.9, 0.9, 300)
        pcm.write_pcm(self.path("c.pcm"), x, dtype="s24le")
        y = pcm.read_pcm(self.path("c.pcm"), channels=1, dtype="s24le")
        np.testing.assert_allclose(y, x, atol=1.0 / (1 << 23) + 1e-12)

    def test_clipping_on_write(self):
        x = np.array([2.0, -2.0, 0.5])
        pcm.write_pcm(self.path("d.pcm"), x, dtype="s16le")
        y = pcm.read_pcm(self.path("d.pcm"), dtype="s16le")
        self.assertLessEqual(y.max(), 32767.0 / 32768.0)
        self.assertGreaterEqual(y.min(), -1.0)

    def test_interleaved_layout(self):
        x = np.arange(12, dtype=float).reshape(4, 3) / 100.0
        pcm.write_pcm(self.path("e.pcm"), x, dtype="f32le")
        raw = np.fromfile(self.path("e.pcm"), dtype="<f4")
        np.testing.assert_allclose(raw, x.ravel(), atol=1e-7)

    def test_bad_channel_count_raises(self):
        pcm.write_pcm(self.path("f.pcm"), np.zeros(10), dtype="s16le")
        with self.assertRaises(ValueError):
            pcm.read_pcm(self.path("f.pcm"), channels=3, dtype="s16le")


class TestSignals(unittest.TestCase):
    def test_shapes_and_determinism(self):
        a = signals.generate_signal("mixed", n=1000, channels=2, seed=9)
        b = signals.generate_signal("mixed", n=1000, channels=2, seed=9)
        self.assertEqual(a.shape, (1000, 2))
        np.testing.assert_array_equal(a, b)
        mono = signals.generate_signal("sine", n=500, channels=1)
        self.assertEqual(mono.shape, (500,))

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            signals.generate_signal("bogus", n=10)


if __name__ == "__main__":
    unittest.main()
