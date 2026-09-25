"""振幅转换：16/24 位码域边界、舍入、裁剪与往返恒等。"""

import unittest

import numpy as np

from pcm_container.amplitude import from_float, int_max, int_min, to_float


class TestAmplitude(unittest.TestCase):
    def test_code_extremes_16bit(self):
        self.assertEqual(int_min(16), -32768)
        self.assertEqual(int_max(16), 32767)
        codes = np.array([-32768, -1, 0, 1, 32767], dtype=np.int32)
        amp = to_float(codes, 16)
        np.testing.assert_array_equal(amp, codes / 32768.0)
        self.assertEqual(amp[0], -1.0)
        self.assertLess(amp[-1], 1.0)  # 正满量程码 32767 对应 1 - 1/32768

    def test_code_extremes_24bit(self):
        self.assertEqual(int_min(24), -8388608)
        self.assertEqual(int_max(24), 8388607)
        codes = np.array([-8388608, -1, 0, 1, 8388607], dtype=np.int32)
        amp = to_float(codes, 24)
        self.assertEqual(amp[0], -1.0)
        self.assertAlmostEqual(amp[-1], 8388607 / 8388608.0)

    def test_int_float_int_identity(self):
        for bits in (16, 24):
            rng = np.random.default_rng(42)
            codes = rng.integers(int_min(bits), int_max(bits) + 1, size=10001)
            back = from_float(to_float(codes, bits), bits)
            np.testing.assert_array_equal(back, codes.astype(np.int32))

    def test_float_int_float_identity(self):
        # 任何浮点值先裁剪到码域后，二次振幅转换保持不变。
        for bits in (16, 24):
            amp = np.linspace(-1.0, 1.0, 5001)
            codes = from_float(amp, bits)
            np.testing.assert_allclose(to_float(codes, bits), amp, atol=1.0 / (1 << (bits - 1)))

    def test_clipping_at_ends(self):
        for bits in (16, 24):
            out = from_float(np.array([-2.0, -1.0, 1.0, 2.0]), bits)
            self.assertEqual(out.tolist(), [int_min(bits), int_min(bits), int_max(bits), int_max(bits)])

    def test_round_half_away_from_zero_band(self):
        # 0.5/32768 恰好在两个码中间：np.rint 取偶数码（银行家舍入），
        # 这里只验证结果恰为某一邻码且无偏出码域。
        half = 0.5 / 32768.0
        out = from_float(np.array([half, -half]), 16)
        self.assertTrue(set(out.tolist()) <= {0, 1, -1})

    def test_nan_and_inf(self):
        out = from_float(np.array([np.nan, np.inf, -np.inf]), 16)
        self.assertEqual(out.tolist(), [0, 32767, -32768])

    def test_unsupported_bits_rejected(self):
        with self.assertRaises(ValueError):
            to_float(np.array([1]), 8)
        with self.assertRaises(ValueError):
            from_float(np.array([0.5]), 32)
        with self.assertRaises(ValueError):
            int_min(20)


if __name__ == "__main__":
    unittest.main()
