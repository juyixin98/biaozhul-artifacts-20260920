"""Tests for windows and framing / config validation."""

import numpy as np
import unittest

from stft_service import make_window, prepare_config
from stft_service.stft import (
    STFTError,
    number_of_frames,
    overlap_sums,
    pad_signal,
)
from stft_service.windows import WINDOW_NAMES


class WindowTests(unittest.TestCase):
    def test_periodic_hann_endpoints(self):
        w = make_window("hann", 8)
        # periodic convention: w[0] == 0, w[-1] != 0 (would be 0 at n+1)
        self.assertAlmostEqual(w[0], 0.0, places=12)
        self.assertGreater(w[-1], 0.0)
        self.assertTrue(np.all(w >= 0.0))

    def test_window_names_and_shapes(self):
        for name in WINDOW_NAMES:
            w = make_window(name, 32)
            self.assertEqual(w.shape, (32,))
            self.assertEqual(w.dtype, np.float64)

    def test_rect_is_ones(self):
        np.testing.assert_array_equal(make_window("rect", 5), np.ones(5))

    def test_unknown_and_bad_length(self):
        with self.assertRaises(ValueError):
            make_window("nope", 8)
        with self.assertRaises(ValueError):
            make_window("hann", 0)


class ConfigTests(unittest.TestCase):
    def test_valid(self):
        cfg = prepare_config(16, 8)
        self.assertEqual(cfg.n_fft, 16)
        self.assertEqual(cfg.window.shape, (16,))

    def test_hop_too_large(self):
        with self.assertRaises(STFTError):
            prepare_config(16, 17)

    def test_nonpositive(self):
        with self.assertRaises(STFTError):
            prepare_config(0, 4)
        with self.assertRaises(STFTError):
            prepare_config(16, 0)

    def test_bad_pad_mode(self):
        with self.assertRaises(STFTError):
            prepare_config(16, 8, pad_mode="wrap")


class FrameCountTests(unittest.TestCase):
    def test_center_frame_counts_known_values(self):
        cfg = prepare_config(8, 4, center=True)
        # padded length L + 8 (2 * n_fft//2)
        self.assertEqual(number_of_frames(0, cfg), 1 + (8 - 8) // 4)  # 1
        self.assertEqual(number_of_frames(1, cfg), 1 + (9 - 8) // 4)  # 1
        self.assertEqual(number_of_frames(4, cfg), 1 + (12 - 8) // 4)  # 2
        self.assertEqual(number_of_frames(8, cfg), 1 + (16 - 8) // 4)  # 3

    def test_no_center_short_input(self):
        cfg = prepare_config(8, 4, center=False)
        self.assertEqual(number_of_frames(7, cfg), 0)
        self.assertEqual(number_of_frames(8, cfg), 1)
        self.assertEqual(number_of_frames(11, cfg), 1)
        self.assertEqual(number_of_frames(12, cfg), 2)


class PadTests(unittest.TestCase):
    def test_constant_pad(self):
        x = np.ones(5)
        xp = pad_signal(x, 8, "constant")
        self.assertEqual(xp.size, 5 + 8)
        np.testing.assert_array_equal(xp[:4], 0.0)
        np.testing.assert_array_equal(xp[-4:], 0.0)
        np.testing.assert_array_equal(xp[4:9], 1.0)

    def test_reflect_pad_values(self):
        x = np.arange(6.0)
        xp = pad_signal(x, 6, "reflect")  # pad 3 each side
        np.testing.assert_array_equal(xp[:3], [3.0, 2.0, 1.0])
        np.testing.assert_array_equal(xp[-3:], [4.0, 3.0, 2.0])

    def test_reflect_too_short_is_diagnosed(self):
        with self.assertRaises(STFTError):
            pad_signal(np.ones(2), 8, "reflect")

    def test_nfft1_no_padding(self):
        x = np.arange(3.0)
        np.testing.assert_array_equal(pad_signal(x, 1, "constant"), x)


class OverlapSumTests(unittest.TestCase):
    def test_hann_half_hop_cola_constant(self):
        cfg = prepare_config(16, 8, "hann", center=True)
        length = 48
        w = overlap_sums(length, cfg, squared=False)
        # interior COLA sum for periodic Hann at 50% is exactly 1;
        # boundaries [0, hop) and [length-n_fft, length) are ramp regions
        np.testing.assert_allclose(w[8 : length - 16], 1.0, atol=1e-12)

    def test_squared_weight_strictly_positive_in_interior(self):
        cfg = prepare_config(16, 8, "hann", center=True)
        w = overlap_sums(64, cfg, squared=True)
        # boundary zeros are expected (no frame covers position 0);
        # every interior position must have strictly positive weight
        self.assertEqual(w[0], 0.0)
        self.assertTrue(np.all(w[8 : 64 - 16] > 0.0))


if __name__ == "__main__":
    unittest.main()
