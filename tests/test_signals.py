"""合成信号：长度对齐、波形取值、扫频与确定性噪声。"""

import unittest

import numpy as np

from pcm_container.errors import PcmAlignmentError, PcmError
from pcm_container.signals import synthesize


class TestSignals(unittest.TestCase):
    def test_integer_frame_lengths(self):
        sig = synthesize("sine", 8000, 1.0, frequency=440)
        self.assertEqual(sig.shape, (8000,))
        sig = synthesize("sine", 8000, 0.01, frequency=440)
        self.assertEqual(sig.shape, (80,))

    def test_fractional_frames_rejected(self):
        # 8000 * 0.0001 = 0.8 帧，无法对齐
        with self.assertRaises(PcmAlignmentError):
            synthesize("sine", 8000, 0.0001)

    def test_sine_exact_samples(self):
        # 1Hz、4Hz 采样、一个周期：sin([0, pi/2, pi, 3pi/2])
        sig = synthesize("sine", 4, 1.0, frequency=1.0, amplitude=1.0)
        np.testing.assert_allclose(sig, [0.0, 1.0, 0.0, -1.0], atol=1e-12)

    def test_square_levels(self):
        sig = synthesize("square", 100, 1.0, frequency=5.0, amplitude=1.0)
        # 过零点样本身为 0（sin 恰为 0），其余只可能为 +/-1
        self.assertTrue(set(np.unique(sig)) <= {-1.0, 0.0, 1.0})
        self.assertIn(1.0, np.unique(sig))
        self.assertIn(-1.0, np.unique(sig))

    def test_sawtooth_range(self):
        sig = synthesize("sawtooth", 1000, 1.0, frequency=3.0, amplitude=1.0)
        self.assertGreaterEqual(sig.min(), -1.0 - 1e-12)
        self.assertLess(sig.max(), 1.0)

    def test_noise_deterministic(self):
        a = synthesize("noise", 1000, 1.0, seed=123)
        b = synthesize("noise", 1000, 1.0, seed=123)
        c = synthesize("noise", 1000, 1.0, seed=124)
        np.testing.assert_array_equal(a, b)
        self.assertFalse(np.array_equal(a, c))
        self.assertGreaterEqual(a.min(), -1.0)
        self.assertLessEqual(a.max(), 1.0)

    def test_chirp_finite_and_sized(self):
        sig = synthesize("chirp", 8000, 0.5, frequency=100.0, frequency_end=1000.0)
        self.assertEqual(sig.shape, (4000,))
        self.assertTrue(np.all(np.isfinite(sig)))

    def test_chirp_requires_endpoint(self):
        with self.assertRaises(PcmError):
            synthesize("chirp", 8000, 0.5, frequency=100.0)

    def test_unknown_type(self):
        with self.assertRaises(PcmError):
            synthesize("banana", 8000, 1.0)


if __name__ == "__main__":
    unittest.main()
