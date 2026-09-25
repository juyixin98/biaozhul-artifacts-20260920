"""Tests for bulk STFT / iSTFT reconstruction and diagnostics."""

from __future__ import annotations

import numpy as np
import pytest

from streaming_stft.stft import (
    STFTConfig,
    build_grid,
    istft,
    stft,
    window_diagnostic,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("hop", [32, 64, 128])
def test_hann_center_reconstruction(random_signal: np.ndarray, hop: int) -> None:
    cfg = STFTConfig(nfft=256, hop=hop, window="hann", pad_mode="reflect")
    out = stft(random_signal, cfg)
    rec = istft(out)
    assert rec.covered
    assert rec.signal.shape == random_signal.shape
    assert np.allclose(rec.signal, random_signal, atol=1e-10, rtol=0.0)


def test_hann_hop_equals_nfft_diagnosed_unreconstructible(
    random_signal: np.ndarray,
) -> None:
    # hop == nfft with a window that vanishes at its edges cannot cover
    # every sample; the service must say so rather than silently zeroing.
    cfg = STFTConfig(nfft=256, hop=256, window="hann", pad_mode="reflect")
    rec = istft(stft(random_signal, cfg))
    assert not rec.covered
    assert rec.zero_weight.size > 0
    assert "NOT RECONSTRUCTIBLE" in rec.diagnostics


@pytest.mark.parametrize("window", ["hann", "hamming", "blackman"])
def test_window_variants_reconstruct(random_signal: np.ndarray, window: str) -> None:
    cfg = STFTConfig(nfft=256, hop=64, window=window, pad_mode="constant")
    rec = istft(stft(random_signal, cfg))
    assert np.allclose(rec.signal, random_signal, atol=1e-9, rtol=0.0)


@pytest.mark.parametrize("pad_mode", ["constant", "edge", "reflect"])
def test_all_pad_modes_reconstruct(random_signal: np.ndarray, pad_mode: str) -> None:
    cfg = STFTConfig(nfft=128, hop=64, pad_mode=pad_mode)
    rec = istft(stft(random_signal, cfg))
    assert np.allclose(rec.signal, random_signal, atol=1e-10, rtol=0.0)


def test_empty_signal() -> None:
    cfg = STFTConfig()
    out = stft(np.zeros(0), cfg)
    assert out.n_frames == 0
    assert out.spectrogram.shape == (0, 129)
    rec = istft(out)
    assert rec.signal.shape == (0,)


@pytest.mark.parametrize("n", [1, 2, 127, 128, 129, 255])
def test_short_inputs(n: int, rng: np.random.Generator) -> None:
    x = rng.standard_normal(n)
    cfg = STFTConfig(nfft=256, hop=128, pad_mode="edge")
    rec = istft(stft(x, cfg))
    assert rec.signal.shape == (n,)
    assert np.allclose(rec.signal, x, atol=1e-10)


def test_spectrogram_shapes() -> None:
    cfg = STFTConfig(nfft=100, hop=30, pad_mode="constant")
    out = stft(np.zeros(500), cfg)
    assert out.spectrogram.shape == (out.n_frames, 51)
    assert np.iscomplexobj(out.spectrogram)


def test_zero_weight_positions_reported_no_center() -> None:
    # Hann window vanishes at index 0; with no overlap (hop == nfft) and
    # no centering, frame start samples cannot be reconstructed.
    cfg = STFTConfig(nfft=32, hop=32, window="hann", center=False)
    x = np.ones(100)
    out = stft(x, cfg)
    rec = istft(out)
    assert not rec.covered
    assert rec.zero_weight.size > 0
    # Frame starts at 0, 32, 64, 96 -> indices 0, 32, 64, 96 are zero-weight.
    assert set(rec.zero_weight.tolist()) >= {0, 32, 64, 96}
    assert "NOT RECONSTRUCTIBLE" in rec.diagnostics


def test_center_does_not_cure_non_overlapping_hann() -> None:
    # Centering only shifts the boundary; hop == nfft with a Hann window
    # still leaves zero-weight samples inside the signal near both edges.
    cfg = STFTConfig(nfft=32, hop=32, window="hann", center=True, pad_mode="constant")
    x = np.ones(100)
    rec = istft(stft(x, cfg))
    assert not rec.covered
    assert rec.zero_weight.size >= 1


def test_rect_half_overlap_constant_weight() -> None:
    cfg = STFTConfig(nfft=32, hop=16, window="rect", center=False)
    x = np.ones(200)
    grid, pad_left, n_frames = build_grid(x, cfg)
    diag = window_diagnostic(cfg, grid.shape[0], n_frames, 200, pad_left)
    assert diag.covered
    # Interior samples are covered by exactly two rectangular windows.
    assert diag.weight_constant_value == pytest.approx(2.0)


def test_hann_quarter_stride_constant_weight() -> None:
    # Periodic Hann: sum of w^2 at 75% overlap (hop = nfft/4) is constant
    # in the interior, equal to mean(w^2) * nfft / hop = 3/8 * 4 = 1.5.
    # With center=False the very first sample still has zero weight because
    # w[0] == 0, so full-signal "covered" is False while the interior is COLA.
    cfg = STFTConfig(nfft=32, hop=8, window="hann", center=False)
    x = np.ones(400)
    grid, pad_left, n_frames = build_grid(x, cfg)
    diag = window_diagnostic(cfg, grid.shape[0], n_frames, 400, pad_left)
    assert not diag.covered
    assert diag.weight_is_constant
    assert diag.weight_constant_value == pytest.approx(1.5, rel=1e-9)
    assert int(diag.zero_positions[0]) == 0


def test_non_constant_weight_still_reconstructs() -> None:
    # Non-COLA overlap: sample-wise normalization must still invert exactly.
    cfg = STFTConfig(nfft=100, hop=37, window="hann", pad_mode="constant")
    rng = np.random.default_rng(3)
    x = rng.standard_normal(777)
    rec = istft(stft(x, cfg))
    assert "NOT constant" in rec.diagnostics
    assert rec.covered
    assert np.allclose(rec.signal, x, atol=1e-10)


def test_custom_window_array() -> None:
    cfg = STFTConfig(nfft=16, hop=8, window=np.hanning(16 + 1)[:-1])
    x = np.random.default_rng(4).standard_normal(200)
    rec = istft(stft(x, cfg))
    assert np.allclose(rec.signal, x, atol=1e-9)
