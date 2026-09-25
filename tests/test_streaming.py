"""Tests for chunked streaming STFT / iSTFT equivalence with the bulk path."""

from __future__ import annotations

import numpy as np
import pytest

from streaming_stft.stft import STFTConfig, istft, stft
from streaming_stft.streaming import ISTFTStreamer, STFTStreamer

pytestmark = pytest.mark.unit


def _stream_pipeline(
    signal: np.ndarray, config: STFTConfig, splits: list[int]
) -> tuple[np.ndarray, np.ndarray]:
    analyzer = STFTStreamer(config)
    synthesizer = ISTFTStreamer(config)
    spectra: list[np.ndarray] = []
    rebuilt: list[np.ndarray] = []
    pos = 0
    index = 0
    while pos < signal.shape[0]:
        size = splits[index % len(splits)]
        index += 1
        block = signal[pos : pos + size]
        pos += block.shape[0]
        frames = analyzer.push(block)
        if frames.shape[0]:
            spectra.append(frames)
            rebuilt.append(synthesizer.push_frames(frames))
    tail = analyzer.flush()
    if tail.shape[0]:
        spectra.append(tail)
        rebuilt.append(synthesizer.push_frames(tail))
    rebuilt.append(synthesizer.flush(signal.shape[0]))
    all_spectra = (
        np.concatenate(spectra)
        if spectra
        else np.zeros((0, config.nfft // 2 + 1), dtype=np.complex128)
    )
    signal_out = np.concatenate(rebuilt) if rebuilt else np.zeros(0)
    return all_spectra, signal_out


@pytest.mark.parametrize("chunk", [1, 7, 31, 128, 129, 1000])
def test_streaming_frames_bit_identical(
    random_signal: np.ndarray, default_config: STFTConfig, chunk: int
) -> None:
    bulk = stft(random_signal, default_config)
    spectra, _ = _stream_pipeline(random_signal, default_config, [chunk])
    assert spectra.shape == bulk.spectrogram.shape
    assert np.array_equal(spectra, bulk.spectrogram)


@pytest.mark.parametrize("chunk", [1, 37, 256, 5000])
def test_streaming_rebuild_matches_bulk(
    random_signal: np.ndarray, default_config: STFTConfig, chunk: int
) -> None:
    bulk_rec = istft(stft(random_signal, default_config)).signal
    _, rebuilt = _stream_pipeline(random_signal, default_config, [chunk])
    assert rebuilt.shape == bulk_rec.shape
    assert np.array_equal(rebuilt, bulk_rec)


def test_irregular_schedule_with_tail_block(
    random_signal: np.ndarray, default_config: STFTConfig
) -> None:
    # 137, 363, 1, ... leaves an irregular tail block.
    splits = [137, 363, 1, 499, 3]
    bulk_rec = istft(stft(random_signal, default_config)).signal
    spectra, rebuilt = _stream_pipeline(random_signal, default_config, splits)
    assert np.array_equal(rebuilt, bulk_rec)


@pytest.mark.parametrize("n", [0, 1, 5, 127, 128, 129, 255, 256, 257])
def test_short_input_lengths(n: int, rng: np.random.Generator) -> None:
    x = rng.standard_normal(n)
    cfg = STFTConfig(nfft=256, hop=128, pad_mode="edge")
    bulk_rec = istft(stft(x, cfg)).signal
    _, rebuilt = _stream_pipeline(x, cfg, [1, 7, 100])
    assert rebuilt.shape == (n,)
    assert np.array_equal(rebuilt, bulk_rec)


@pytest.mark.parametrize("nfft,hop", [(64, 64), (100, 100), (300, 70), (256, 200)])
def test_tail_block_across_hop_configs(nfft: int, hop: int) -> None:
    rng = np.random.default_rng(9)
    x = rng.standard_normal(700)
    cfg = STFTConfig(nfft=nfft, hop=hop, window="hann", pad_mode="constant")
    bulk_rec = istft(stft(x, cfg)).signal
    _, rebuilt = _stream_pipeline(x, cfg, [nfft - 1, hop + 1, 13])
    assert np.allclose(rebuilt, bulk_rec, atol=1e-12)


def test_streaming_zero_weight_matches_bulk() -> None:
    cfg = STFTConfig(nfft=32, hop=32, window="hann", center=False)
    x = np.ones(128)
    bulk = stft(x, cfg)
    bulk_zero = set(istft(bulk).zero_weight.tolist())

    analyzer = STFTStreamer(cfg)
    synthesizer = ISTFTStreamer(cfg)
    for i in range(0, 128, 10):
        frames = analyzer.push(x[i : i + 10])
        if frames.shape[0]:
            synthesizer.push_frames(frames)
    tail = analyzer.flush()
    if tail.shape[0]:
        synthesizer.push_frames(tail)
    synthesizer.flush(128)
    assert set(synthesizer.zero_weight_positions.tolist()) == bulk_zero


def test_double_flush_raises(default_config: STFTConfig) -> None:
    analyzer = STFTStreamer(default_config)
    analyzer.flush()
    with pytest.raises(RuntimeError, match="flush"):
        analyzer.flush()

    synthesizer = ISTFTStreamer(default_config)
    synthesizer.flush(0)
    with pytest.raises(RuntimeError, match="flush"):
        synthesizer.flush(0)


def test_push_after_flush_raises(default_config: STFTConfig) -> None:
    analyzer = STFTStreamer(default_config)
    analyzer.flush()
    with pytest.raises(RuntimeError, match="flush"):
        analyzer.push(np.ones(10))


def test_streaming_sample_by_sample_reconstruction() -> None:
    rng = np.random.default_rng(11)
    x = rng.standard_normal(500)
    cfg = STFTConfig(nfft=64, hop=16, pad_mode="reflect")
    _, rebuilt = _stream_pipeline(x, cfg, [1])
    assert np.allclose(rebuilt, x, atol=1e-10)


def test_total_emitted_sample_count() -> None:
    cfg = STFTConfig(nfft=64, hop=32, pad_mode="edge")
    x = np.random.default_rng(5).standard_normal(333)
    analyzer = STFTStreamer(cfg)
    synthesizer = ISTFTStreamer(cfg)
    emitted = 0
    for i in range(0, x.shape[0], 50):
        frames = analyzer.push(x[i : i + 50])
        if frames.shape[0]:
            emitted += synthesizer.push_frames(frames).shape[0]
    tail = analyzer.flush()
    if tail.shape[0]:
        emitted += synthesizer.push_frames(tail).shape[0]
    emitted += synthesizer.flush(x.shape[0]).shape[0]
    assert emitted == x.shape[0]
