"""Error paths and boundary cases for robustness coverage."""

from __future__ import annotations

import numpy as np
import pytest

from streaming_stft.signals import SyntheticSpec, generate_signal
from streaming_stft.stft import (
    STFTConfig,
    STFTOutput,
    istft,
    stft,
    validate_config,
)
from streaming_stft.streaming import ISTFTStreamer, STFTStreamer
from streaming_stft.windows import get_window
from streaming_stft.pcmio import PCMSource

pytestmark = pytest.mark.unit


def test_validate_nfft_positive() -> None:
    with pytest.raises(ValueError, match="nfft"):
        validate_config(STFTConfig(nfft=0))


def test_validate_hop_positive() -> None:
    with pytest.raises(ValueError, match="hop"):
        validate_config(STFTConfig(nfft=32, hop=0))


def test_validate_bad_pad_mode() -> None:
    with pytest.raises(ValueError, match="pad_mode"):
        validate_config(STFTConfig(pad_mode="wrap"))


def test_custom_window_wrong_length() -> None:
    with pytest.raises(ValueError, match="length nfft"):
        stft(np.zeros(100), STFTConfig(nfft=32, hop=16, window=np.ones(10)))


def test_window_from_list() -> None:
    w = STFTConfig(nfft=8, window=[1.0] * 8).resolved_window()
    assert w.shape == (8,)


def test_window_bad_name_type() -> None:
    with pytest.raises(TypeError):
        get_window(123, 8)  # type: ignore[arg-type]


def test_stft_rejects_2d_signal() -> None:
    with pytest.raises(ValueError, match="1-D"):
        stft(np.zeros((3, 3)))


def test_istft_empty_signal() -> None:
    out = stft(np.zeros(0))
    rec = istft(out)
    assert rec.signal.shape == (0,)
    assert rec.covered is False


def test_istft_requested_length_too_long() -> None:
    out = stft(np.zeros(200))
    with pytest.raises(ValueError, match="exceeds"):
        istft(out, signal_length=10**6)


def test_istft_bin_mismatch() -> None:
    good = stft(np.zeros(200))
    broken = STFTOutput(
        spectrogram=np.zeros((2, 99), dtype=np.complex128),
        n_samples=good.n_samples,
        n_frames=2,
        pad_left=good.pad_left,
        pad_right=good.pad_right,
        window=good.window,
        config=good.config,
    )
    with pytest.raises(ValueError, match="bin count"):
        istft(broken)


def test_signal_validation_errors() -> None:
    with pytest.raises(ValueError, match="duration"):
        generate_signal(SyntheticSpec(duration_s=0))
    with pytest.raises(ValueError, match="sample_rate"):
        generate_signal(SyntheticSpec(sample_rate=0))
    with pytest.raises(ValueError, match="amplitudes"):
        generate_signal(
            SyntheticSpec(frequencies=(1.0, 2.0), amplitudes=(1.0,))
        )


def test_signal_chirp_and_noise() -> None:
    x = generate_signal(
        SyntheticSpec(
            duration_s=0.02,
            sample_rate=8000,
            frequencies=(100.0,),
            chirp_to=500.0,
            noise_std=0.01,
            seed=7,
        )
    )
    assert x.shape == (160,)
    # Determinism: same seed -> same noise.
    y = generate_signal(
        SyntheticSpec(
            duration_s=0.02,
            sample_rate=8000,
            frequencies=(100.0,),
            chirp_to=500.0,
            noise_std=0.01,
            seed=7,
        )
    )
    assert np.array_equal(x, y)


def test_pcm_source_sample_count(tmp_path) -> None:
    p = tmp_path / "x.pcm"
    p.write_bytes(b"\x00" * 64)
    assert PCMSource(str(p), "int16").sample_count() == 32


def test_streamer_left_edge_padding_modes() -> None:
    x = np.arange(1, 300, dtype=np.float64)
    for mode in ["constant", "edge", "reflect"]:
        cfg = STFTConfig(nfft=64, hop=16, pad_mode=mode)
        analyzer = STFTStreamer(cfg)
        frames = analyzer.push(x[:5])
        assert frames.shape[0] == 0  # not enough for first frame
        frames = analyzer.push(x[5:])
        tail = analyzer.flush()
        assert frames.shape[0] + tail.shape[0] == stft(x, cfg).n_frames


def test_streamer_short_signal_no_frames() -> None:
    # Without centering, a signal shorter than one window yields no frames;
    # every sample is then reported zero-weight (same as bulk).
    cfg = STFTConfig(nfft=64, hop=16, center=False)
    analyzer = STFTStreamer(cfg)
    assert analyzer.push(np.arange(3)).shape[0] == 0
    assert analyzer.flush().shape[0] == 0
    synthesizer = ISTFTStreamer(cfg)
    out = synthesizer.flush(3)
    assert out.shape == (3,)
    assert np.array_equal(synthesizer.zero_weight_positions, [0, 1, 2])


def test_streamer_short_centered_signal_one_frame() -> None:
    # Edge-centering pads a short signal to exactly one window, which
    # reconstructs perfectly by replication.
    cfg = STFTConfig(nfft=64, hop=16, pad_mode="edge")
    analyzer = STFTStreamer(cfg)
    assert analyzer.push(np.full(3, 0.7)).shape[0] == 0
    tail = analyzer.flush()
    assert tail.shape[0] == 1
    synthesizer = ISTFTStreamer(cfg)
    synthesizer.push_frames(tail)
    out = synthesizer.flush(3)
    assert out.shape == (3,)
    assert np.allclose(out, 0.7)


def test_streamer_reflect_too_short_raises() -> None:
    cfg = STFTConfig(nfft=64, hop=16, pad_mode="reflect")
    analyzer = STFTStreamer(cfg)
    analyzer.push(np.arange(3))
    with pytest.raises(ValueError, match="reflect padding"):
        analyzer.flush()


def test_synthesizer_double_flush() -> None:
    synth = ISTFTStreamer()
    synth.flush(0)
    with pytest.raises(RuntimeError, match="flush"):
        synth.flush(0)


def test_synthesizer_push_after_flush() -> None:
    cfg = STFTConfig(nfft=256, hop=128, pad_mode="edge")
    synth = ISTFTStreamer(cfg)
    analyzer = STFTStreamer(cfg)
    x = np.ones(300)
    frames = analyzer.push(x)
    synth.push_frames(frames)
    tail = analyzer.flush()
    synth.push_frames(tail)
    synth.flush(300)
    with pytest.raises(RuntimeError, match="flush"):
        synth.push_frame(frames[0])
