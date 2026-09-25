"""Unit tests for the framing grid and frame count formula."""

from __future__ import annotations

import numpy as np
import pytest

from streaming_stft.stft import STFTConfig, frame_plan, build_grid

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("nfft,hop", [(256, 128), (256, 64), (100, 100), (300, 70)])
def test_frame_plan_matches_actual_framing(nfft: int, hop: int) -> None:
    cfg = STFTConfig(nfft=nfft, hop=hop, pad_mode="constant")
    for n in [0, 1, 128, 257, 1000]:
        base_len, n_frames, grid_len = frame_plan(cfg, n)
        if n_frames == 0:
            assert n == 0 or base_len < nfft
            continue
        # Last frame window must reach the signal end.
        core_end = nfft // 2 + n
        assert (n_frames - 1) * hop + nfft >= core_end
        # All planned frames fit inside the declared grid.
        assert grid_len == (n_frames - 1) * hop + nfft
        # Independent recomputation of the two frame-count sources.
        f_in_base = max(0, (base_len - nfft) // hop + 1)
        f_covering = int(np.ceil((core_end - nfft) / hop)) + 1
        assert n_frames == max(f_in_base, f_covering)


@pytest.mark.parametrize("pad_mode", ["constant", "edge", "reflect"])
def test_build_grid_dimensions_and_zero_tail(pad_mode: str) -> None:
    cfg = STFTConfig(nfft=64, hop=64, pad_mode=pad_mode)
    x = np.arange(100, dtype=np.float64)
    grid, pad_left, n_frames = build_grid(x, cfg)
    assert pad_left == 32
    assert n_frames == 3
    # Frames at 0, 64, 128; last ends at 192; core ends at 132.
    assert grid.shape[0] == 192
    # The zero-filled tail (everything after the 32+100+32 center pad) is zero.
    assert np.all(grid[2 * 32 + 100 :] == 0)


def test_reflect_padding_matches_numpy() -> None:
    cfg = STFTConfig(nfft=64, hop=32, pad_mode="reflect")
    x = np.arange(100, dtype=np.float64)
    grid, pad_left, n_frames = build_grid(x, cfg)
    expected = np.pad(x, (32, 32), mode="reflect")
    assert np.array_equal(grid[: expected.shape[0]], expected)


def test_short_signal_reflect_raises() -> None:
    cfg = STFTConfig(nfft=64, hop=32, pad_mode="reflect")
    with pytest.raises(ValueError, match="reflect padding"):
        build_grid(np.zeros(32), cfg)


def test_hop_greater_than_nfft_rejected() -> None:
    from streaming_stft.stft import validate_config

    cfg = STFTConfig(nfft=32, hop=33)
    with pytest.raises(ValueError, match="hop"):
        validate_config(cfg)
