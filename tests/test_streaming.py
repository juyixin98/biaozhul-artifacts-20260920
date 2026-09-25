"""Streaming vs whole-signal equivalence, incl. tail blocks and short input."""

import numpy as np
import unittest

from stft_service import (
    StreamingISTFT,
    StreamingSTFT,
    istft,
    prepare_config,
    stft,
)
from stft_service.signals import synthesize
from stft_service.stft import STFTError


def stream_forward(x, cfg, chunk_sizes):
    """Run the streaming forward transform; return concatenated spectrum."""
    streamer = StreamingSTFT(cfg)
    parts = []
    pos = 0
    i = 0
    while pos < x.size:
        size = chunk_sizes[i % len(chunk_sizes)]
        take = min(size, x.size - pos)
        parts.append(streamer.push(x[pos : pos + take]))
        pos += take
        i += 1
    parts.append(streamer.finish())
    return np.concatenate(parts, axis=0)


def stream_inverse(spec, cfg, signal_length, frame_group):
    """Run the streaming inverse, feeding frames in groups of frame_group."""
    istream = StreamingISTFT(cfg)
    ready = []
    for start in range(0, spec.shape[0], frame_group):
        istream.push(spec[start : start + frame_group])
        ready.append(istream.take_ready())
    y, info = istream.finish(signal_length=signal_length)
    return y, info, np.concatenate(ready) if ready else np.empty(0)


class StreamingForwardTests(unittest.TestCase):
    def assertStreamMatchesWhole(self, x, n_fft, hop, chunk_sizes,
                                 window="hann", center=True):
        cfg = prepare_config(n_fft, hop, window_name=window, center=center)
        whole = stft(x, cfg)
        streamed = stream_forward(x, cfg, chunk_sizes)
        self.assertEqual(
            streamed.shape, whole.shape,
            f"frame count mismatch {streamed.shape} vs {whole.shape}",
        )
        np.testing.assert_allclose(
            streamed, whole, rtol=0, atol=1e-12, strict=True
        )

    def test_various_chunk_sizes(self):
        x = synthesize(2000)
        for chunks in ([1], [7], [64], [128], [500], [37, 100, 3]):
            self.assertStreamMatchesWhole(x, 256, 128, chunks)

    def test_chunk_smaller_than_hop(self):
        x = synthesize(1000)
        self.assertStreamMatchesWhole(x, 64, 32, [5])

    def test_chunk_larger_than_signal(self):
        x = synthesize(300)
        self.assertStreamMatchesWhole(x, 128, 64, [10000])

    def test_short_inputs(self):
        for n in (0, 1, 2, 5, 63):
            x = synthesize(n, noise_std=0.0)
            self.assertStreamMatchesWhole(x, 64, 32, [7])

    def test_tail_block_every_residue(self):
        # lengths covering every residue class of the hop
        for extra in range(32):
            x = synthesize(480 + extra, noise_std=0.0)
            self.assertStreamMatchesWhole(x, 128, 32, [77])

    def test_center_false(self):
        x = synthesize(500)
        self.assertStreamMatchesWhole(x, 64, 32, [100], center=False)

    def test_reflect_rejected_in_stream(self):
        cfg = prepare_config(64, 32, pad_mode="reflect")
        with self.assertRaises(STFTError):
            StreamingSTFT(cfg)

    def test_push_after_finish_rejected(self):
        cfg = prepare_config(64, 32)
        s = StreamingSTFT(cfg)
        s.finish()
        with self.assertRaises(STFTError):
            s.push(np.zeros(10))
        with self.assertRaises(STFTError):
            s.finish()


class StreamingInverseTests(unittest.TestCase):
    def assertInverseMatchesWhole(self, x, n_fft, hop, frame_group,
                                  chunk_sizes=(50,), window="hann"):
        cfg = prepare_config(n_fft, hop, window_name=window)
        spec = stft(x, cfg)
        y_whole, info_whole = istft(spec, cfg, signal_length=x.size)
        y_stream, info_stream, _ = stream_inverse(
            spec, cfg, x.size, frame_group
        )
        self.assertEqual(info_stream["reconstruction_possible"],
                         info_whole["reconstruction_possible"])
        np.testing.assert_allclose(y_stream, y_whole, rtol=0, atol=1e-12)
        np.testing.assert_allclose(y_stream, x, atol=1e-10)

    def test_frame_group_sizes(self):
        x = synthesize(1500)
        for grp in (1, 2, 5, 100):
            self.assertInverseMatchesWhole(x, 128, 64, grp)

    def test_short_inputs(self):
        for n in (0, 1, 3, 70):
            x = synthesize(n, noise_std=0.0)
            self.assertInverseMatchesWhole(x, 64, 32, 1)

    def test_zero_weight_positions_flagged_in_stream(self):
        # hop == n_fft with Hann: zero weights at frame starts
        cfg = prepare_config(64, 64, "hann")
        x = synthesize(256, frequencies=(440.0,), amplitudes=(1.0,),
                       noise_std=0.0)
        spec = stft(x, cfg)
        y_whole, info_whole = istft(spec, cfg, signal_length=256)
        y_stream, info_stream, _ = stream_inverse(spec, cfg, 256, 3)
        self.assertFalse(info_stream["reconstruction_possible"])
        self.assertEqual(
            info_stream["zero_weight_positions"],
            info_whole["zero_weight_positions"],
        )
        np.testing.assert_array_equal(y_stream, y_whole)

    def test_ready_samples_prefix_of_final(self):
        # samples returned by take_ready must be a prefix of the final signal
        cfg = prepare_config(64, 32)
        x = synthesize(400)
        spec = stft(x, cfg)
        istream = StreamingISTFT(cfg)
        ready = []
        for m in range(spec.shape[0]):
            istream.push(spec[m : m + 1])
            ready.append(istream.take_ready())
        y_final, _ = istream.finish(signal_length=400)
        ready_all = np.concatenate(ready)
        # ready prefix (padded coords) minus the n_fft//2 left pad must be a
        # prefix of the final trimmed signal
        cut = 32
        self.assertGreaterEqual(ready_all.size, cut)
        prefix = ready_all[cut:]
        np.testing.assert_allclose(
            prefix, y_final[: prefix.size], rtol=0, atol=1e-12
        )

    def test_finish_requires_length_when_center(self):
        cfg = prepare_config(64, 32)
        istream = StreamingISTFT(cfg)
        with self.assertRaises(STFTError):
            istream.finish()


class EndToEndStreamTests(unittest.TestCase):
    def test_stream_forward_then_stream_inverse(self):
        cfg = prepare_config(128, 64)
        x = synthesize(1500)
        spec = stream_forward(x, cfg, [31, 64, 100])
        y, info, _ = stream_inverse(spec, cfg, x.size, 4)
        self.assertTrue(info["reconstruction_possible"])
        np.testing.assert_allclose(y, x, atol=1e-10)


if __name__ == "__main__":
    unittest.main()
