"""Whole-signal STFT <-> iSTFT round-trip and reconstruction diagnostics."""

import numpy as np
import unittest

from stft_service import prepare_config, stft, istft
from stft_service.signals import synthesize
from stft_service.stft import cola_diagnostic


class RoundTripTests(unittest.TestCase):
    def assertRoundTrips(self, x, n_fft, hop, window="hann",
                         center=True, pad_mode="constant", tol=1e-10):
        cfg = prepare_config(n_fft, hop, window_name=window, center=center,
                             pad_mode=pad_mode)
        spec = stft(x, cfg)
        y, info = istft(spec, cfg, signal_length=x.size)
        self.assertTrue(
            info["reconstruction_possible"],
            f"zero weight positions: {info['zero_weight_positions'][:5]}",
        )
        self.assertEqual(y.shape, x.shape)
        err = np.max(np.abs(y - x)) if x.size else 0.0
        self.assertLess(err, tol, f"max abs error {err:.3e} >= {tol}")
        return spec, y, info

    def test_typical_signal_half_hop(self):
        x = synthesize(4096)
        self.assertRoundTrips(x, 256, 128)

    def test_75_percent_overlap(self):
        x = synthesize(2000)
        self.assertRoundTrips(x, 256, 64)

    def test_odd_hop(self):
        x = synthesize(1000, noise_std=0.0)
        self.assertRoundTrips(x, 100, 30)

    def test_nfft_equals_hop_rect(self):
        # no overlap, rect window: exact when the signal length is covered
        # by complete frames (no padding ramp to mis-reconstruct)
        x = synthesize(512, noise_std=0.0)
        self.assertRoundTrips(x, 64, 64, window="rect")

    def test_hamming_half_hop(self):
        # Hamming at 50% is COLA, so NOLA holds; reconstruction exact
        x = synthesize(1234)
        self.assertRoundTrips(x, 64, 32, window="hamming")

    def test_reflect_padding_roundtrip(self):
        x = synthesize(777)
        self.assertRoundTrips(x, 64, 32, pad_mode="reflect")

    def test_reflect_padding_exact_fit_length(self):
        # smallest signal reflect padding allows: L + 1 == n_fft
        x = synthesize(63)
        self.assertRoundTrips(x, 64, 32, pad_mode="reflect")

    def test_center_false_reconstructs_interior(self):
        x = synthesize(500)
        cfg = prepare_config(64, 32, center=False)
        spec = stft(x, cfg)
        y, info = istft(spec, cfg)
        # periodic Hann w[0] = 0 and, with no padding, the first sample is
        # covered only at frame start 0 -> intrinsic zero weight there.
        # Everything else in the covered region rebuilds exactly.
        self.assertEqual(y.size, (1 + (500 - 64) // 32 - 1) * 32 + 64)
        self.assertEqual(info["zero_weight_positions"], [0])
        np.testing.assert_allclose(y[1:], x[1 : y.size], atol=1e-10)

    def test_noise_signal_roundtrip(self):
        rng = np.random.default_rng(7)
        x = rng.standard_normal(3000)
        self.assertRoundTrips(x, 512, 128)


class ShortInputTests(unittest.TestCase):
    def test_length_zero(self):
        cfg = prepare_config(8, 4, center=True)
        spec = stft(np.zeros(0), cfg)
        self.assertEqual(spec.shape[0], number_of_frames0(cfg))
        y, info = istft(spec, cfg, signal_length=0)
        self.assertEqual(y.size, 0)
        self.assertTrue(info["reconstruction_possible"])

    def test_length_one(self):
        x = np.array([0.5])
        cfg = prepare_config(8, 4, center=True)
        spec = stft(x, cfg)
        self.assertEqual(spec.shape[0], 1)
        y, info = istft(spec, cfg, signal_length=1)
        np.testing.assert_allclose(y, x, atol=1e-12)

    def test_length_two_to_seven(self):
        for n in range(2, 8):
            x = synthesize(n, frequencies=(100.0,), amplitudes=(1.0,),
                           noise_std=0.0)
            cfg = prepare_config(8, 4, center=True)
            spec = stft(x, cfg)
            y, info = istft(spec, cfg, signal_length=n)
            self.assertEqual(y.size, n)
            np.testing.assert_allclose(y, x, atol=1e-10,
                                       err_msg=f"length {n}")

    def test_shorter_than_window_constant_pad(self):
        x = synthesize(50, noise_std=0.0)
        cfg = prepare_config(256, 128, center=True)
        spec = stft(x, cfg)
        y, info = istft(spec, cfg, signal_length=50)
        # signal lies near the window edge: weight is tiny but > 0 for Hann
        # at hop n_fft/2 -> check reconstructability flag and values
        self.assertEqual(y.shape, x.shape)
        # weight at edges of center-padded signal for hop=n_fft/2 Hann:
        # sample 0 is covered by frames 0 and n_fft/2/hop ... see explicit
        # min-weight assertion in diagnostic tests; values:
        np.testing.assert_allclose(y, x, atol=1e-8)


def number_of_frames0(cfg):
    from stft_service import number_of_frames
    return number_of_frames(0, cfg)


class TailBlockTests(unittest.TestCase):
    """Signals whose length hits every residue class mod hop must rebuild."""

    def test_all_residues(self):
        base = 1000
        for extra in range(0, 32):
            n = base + extra
            x = synthesize(n, noise_std=0.0)
            cfg = prepare_config(128, 32, center=True)
            spec = stft(x, cfg)
            y, info = istft(spec, cfg, signal_length=n)
            self.assertTrue(info["reconstruction_possible"])
            np.testing.assert_allclose(
                y, x, atol=1e-10, err_msg=f"n={n}"
            )

    def test_exact_multiple_lengths(self):
        for n in (256, 512, 1024):
            x = synthesize(n, noise_std=0.0)
            cfg = prepare_config(256, 128, center=True)
            y, info = istft(stft(x, cfg), cfg, signal_length=n)
            np.testing.assert_array_equal(y.shape, x.shape)
            np.testing.assert_allclose(y, x, atol=1e-10)


class DiagnosticTests(unittest.TestCase):
    def test_hann_half_hop_nola_holds(self):
        cfg = prepare_config(64, 32, "hann")
        diag = cola_diagnostic(cfg, length=1000)
        self.assertTrue(diag["cola_holds"])
        self.assertTrue(diag["nola_holds"])
        self.assertEqual(diag["nola_zero_positions_periodic"], [])
        # finite buffer: boundary zeros exist but are trimmed away
        self.assertIn(0, diag["zero_weight_positions_all"])

    def test_hann_hop_equals_nfft_has_zero_weights(self):
        cfg = prepare_config(64, 64, "hann")
        diag = cola_diagnostic(cfg, length=1024)
        self.assertFalse(diag["nola_holds"])
        # periodic lattice: only residue class 0 has zero weight
        self.assertEqual(diag["nola_zero_positions_periodic"], [0])

        x = synthesize(512, frequencies=(440.0,), amplitudes=(1.0,),
                       noise_std=0.0)
        spec = stft(x, cfg)
        y, info = istft(spec, cfg, signal_length=512)
        self.assertFalse(info["reconstruction_possible"])
        bad = info["zero_weight_positions"]
        # center padding cuts n_fft//2=32; zero frame starts at padded
        # positions 64, 128, ... map to signal positions 32, 96, ...
        self.assertNotIn(0, bad)
        self.assertIn(32, bad)
        self.assertIn(96, bad)
        # output is exactly zero at the bad positions, not NaN
        self.assertTrue(np.all(np.isfinite(y)))
        self.assertEqual(y[32], 0.0)
        # between zeros the signal IS reconstructed (weight > 0)
        good = np.ones(512, bool)
        good[bad] = False
        np.testing.assert_allclose(y[good], x[good], atol=1e-10)

    def test_rect_hop_equals_nfft_reconstructs(self):
        cfg = prepare_config(16, 16, "rect")
        diag = cola_diagnostic(cfg, length=320)
        self.assertTrue(diag["nola_holds"])
        self.assertTrue(diag["cola_holds"])


class SpectrumShapeTests(unittest.TestCase):
    def test_shape_and_dtype(self):
        cfg = prepare_config(16, 8)
        spec = stft(synthesize(100), cfg)
        self.assertEqual(spec.shape[1], 9)
        self.assertEqual(spec.dtype, np.complex128)

    def test_bad_spectrum_shape_rejected(self):
        cfg = prepare_config(16, 8)
        with self.assertRaises(Exception):
            istft(np.zeros((3, 5), dtype=np.complex128), cfg,
                  signal_length=32)


if __name__ == "__main__":
    unittest.main()
