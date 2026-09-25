"""Acceptance tests for the streaming FIR engine.

Covers: whole-signal reference convolution comparison, varied blockings,
switch-during-switch, channel-count changes, per-channel isolation,
empty-block state invariance, and boundary discontinuity detection.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from fir_stream import (
    MultiChannelFIR,
    StreamingFIR,
    boundary_jumps,
    find_discontinuities,
    max_abs_error,
)
from fir_stream import filters, pcm, service, synth


def reference_convolution(x: np.ndarray, h: np.ndarray) -> np.ndarray:
    """Whole-signal 'full' convolution truncated to len(x) — the ground
    truth a streaming engine must reproduce sample-for-sample."""
    return np.convolve(x, h)[: x.size]


def stream_in_blocks(engine, x: np.ndarray, block_size: int) -> np.ndarray:
    parts = []
    for start in range(0, x.size, block_size):
        parts.append(engine.process(x[start : start + block_size]))
    return np.concatenate(parts) if parts else np.zeros(0)


# ----------------------------------------------------------------------
# 1. Streaming vs whole-signal reference, across many block sizes
# ----------------------------------------------------------------------
@pytest.mark.parametrize("block_size", [1, 3, 7, 64, 256, 1000, 4096])
def test_streaming_matches_reference_all_block_sizes(block_size):
    rng = np.random.default_rng(42)
    x = rng.standard_normal(5000)
    h = filters.lowpass(num_taps=63, cutoff_hz=2000.0, sample_rate=48000.0)
    engine = StreamingFIR(h)
    y = stream_in_blocks(engine, x, block_size)
    err = max_abs_error(y, reference_convolution(x, h))
    assert err < 1e-10, f"block_size={block_size}: max abs error {err}"


def test_streaming_matches_reference_nondivisible_blocks():
    rng = np.random.default_rng(7)
    x = rng.standard_normal(1000)
    h = filters.moving_average(17)
    engine = StreamingFIR(h)
    y = stream_in_blocks(engine, x, 128)  # 1000 % 128 != 0
    assert max_abs_error(y, reference_convolution(x, h)) < 1e-12


def test_multichannel_matches_per_channel_reference():
    x = synth.multi_sine(3, 0.25, 48000.0, [440.0, 1200.0, 5000.0], seed=3)
    h = filters.lowpass(95, 1500.0, 48000.0)
    engine = MultiChannelFIR(3, h)
    y = engine.process(x)  # single block
    for ch in range(3):
        assert max_abs_error(y[ch], reference_convolution(x[ch], h)) < 1e-12


# ----------------------------------------------------------------------
# 2. Empty blocks must not change state
# ----------------------------------------------------------------------
def test_empty_block_leaves_state_untouched():
    rng = np.random.default_rng(1)
    h = filters.lowpass(31, 1000.0, 48000.0)
    engine = StreamingFIR(h)
    engine.process(rng.standard_normal(100))
    before = engine.snapshot_state()
    out = engine.process(np.zeros(0))
    assert out.size == 0
    for a, b in zip(before, engine.snapshot_state()):
        np.testing.assert_array_equal(a, b)


def test_empty_block_multichannel_shape_and_state():
    engine = MultiChannelFIR(2, filters.moving_average(9))
    engine.process(np.ones((2, 50)))
    before = [ch.snapshot_state() for ch in (engine.channel(0), engine.channel(1))]
    out = engine.process(np.zeros((2, 0)))
    assert out.shape == (2, 0)
    for idx, ch in enumerate((engine.channel(0), engine.channel(1))):
        for a, b in zip(before[idx], ch.snapshot_state()):
            np.testing.assert_array_equal(a, b)


# ----------------------------------------------------------------------
# 3. Crossfaded switching: correctness and continuity
# ----------------------------------------------------------------------
def _continuity_margin(y: np.ndarray) -> float:
    """Largest sample-to-sample jump in the output."""
    return float(np.max(np.abs(np.diff(y)))) if y.size > 1 else 0.0


def test_crossfade_settles_exactly_on_new_filter():
    rng = np.random.default_rng(5)
    x = rng.standard_normal(8000)
    h1 = filters.lowpass(63, 1000.0, 48000.0)
    h2 = filters.highpass(63, 4000.0, 48000.0)
    fade = 512
    switch_at = 3000

    engine = StreamingFIR(h1)
    y_parts = []
    pos = 0
    block = 256
    switched = False
    while pos < x.size:
        end = min(pos + block, x.size)
        if not switched and end > switch_at:
            y_parts.append(engine.process(x[pos:switch_at]))
            engine.switch(h2, fade_samples=fade)
            y_parts.append(engine.process(x[switch_at:end]))
            switched = True
        else:
            y_parts.append(engine.process(x[pos:end]))
        pos = end
    y = np.concatenate(y_parts)

    # After the fade, output must equal the new filter convolved with the
    # real signal (the lane was primed with input history at switch time).
    steady_start = switch_at + fade
    ref = reference_convolution(x, h2)
    err = max_abs_error(y[steady_start:], ref[steady_start:])
    assert err < 1e-10, f"post-fade error vs new-filter reference: {err}"

    # During the fade the output is a smooth blend: no sample-to-sample
    # jump may exceed what the input itself could produce through either
    # filter (generous bound: 4x the max input step).
    max_input_step = float(np.max(np.abs(np.diff(x))))
    assert _continuity_margin(y) < 4.0 * max_input_step


def test_hard_switch_uses_real_history_not_zeros():
    """fade_samples=0 must still prime the new lane with past input."""
    rng = np.random.default_rng(9)
    x = rng.standard_normal(2000)
    h1 = filters.moving_average(5)
    h2 = filters.delay(20)
    engine = StreamingFIR(h1)
    engine.process(x[:1000])
    engine.switch(h2, fade_samples=0)
    y = engine.process(x[1000:])
    ref = reference_convolution(x, h2)
    assert max_abs_error(y, ref[1000:]) < 1e-12


def test_switch_during_switch():
    """A second switch issued mid-fade must stay continuous and settle on
    the final filter."""
    rng = np.random.default_rng(11)
    x = rng.standard_normal(12000)
    h1 = filters.lowpass(63, 800.0, 48000.0)
    h2 = filters.lowpass(63, 2500.0, 48000.0)
    h3 = filters.highpass(63, 5000.0, 48000.0)

    engine = StreamingFIR(h1)
    engine.process(x[:2000])
    engine.switch(h2, fade_samples=2000)          # fade 1: samples 2000..4000
    engine.process(x[2000:2500])                  # 500 samples into fade 1
    assert engine.fade_in_progress
    engine.switch(h3, fade_samples=1000)          # switch during switch
    y_rest = []
    pos = 2500
    while pos < x.size:
        end = min(pos + 333, x.size)
        y_rest.append(engine.process(x[pos:end]))
        pos = end
    y = np.concatenate(y_rest)

    # Fade 2 ends at sample 3500; afterwards output must match h3 exactly.
    ref = reference_convolution(x, h3)
    err = max_abs_error(y[3500 - 2500 :], ref[3500:])
    assert err < 1e-10, f"post double-switch error: {err}"
    assert not engine.fade_in_progress
    assert engine.num_lanes == 1  # old lanes cleaned up

    max_input_step = float(np.max(np.abs(np.diff(x))))
    assert _continuity_margin(y) < 4.0 * max_input_step


def test_per_channel_switch_isolation():
    """Switching one channel must not perturb the other channel at all."""
    x = synth.multi_sine(2, 0.2, 48000.0, [500.0, 4500.0], seed=8)
    h1 = filters.lowpass(63, 1000.0, 48000.0)
    h2 = filters.highpass(63, 3000.0, 48000.0)
    engine = MultiChannelFIR(2, h1)
    engine.process(x[:, :2000])
    engine.switch(h2, fade_samples=256, channel=0)
    y = engine.process(x[:, 2000:])

    # Channel 1 never switched: exact reference for the whole signal.
    ref1 = reference_convolution(x[1], h1)
    assert max_abs_error(y[1], ref1[2000:]) < 1e-12
    # Channel 0 settles on h2 after its fade.
    ref0 = reference_convolution(x[0], h2)
    assert max_abs_error(y[0][256:], ref0[2000 + 256 :]) < 1e-10


# ----------------------------------------------------------------------
# 4. Channel-count changes
# ----------------------------------------------------------------------
def test_channel_count_change_resets_and_works():
    x2 = synth.multi_sine(2, 0.1, 48000.0, [440.0], seed=2)
    h = filters.moving_average(11)
    engine = MultiChannelFIR(2, h)
    engine.process(x2)

    engine.set_num_channels(3)
    assert engine.num_channels == 3
    x3 = synth.multi_sine(3, 0.1, 48000.0, [440.0], seed=4)
    y = engine.process(x3)
    # State was reset: output equals a fresh engine's reference output.
    for ch in range(3):
        assert max_abs_error(y[ch], reference_convolution(x3[ch], h)) < 1e-12

    engine.set_num_channels(1)
    x1 = synth.multi_sine(1, 0.05, 48000.0, [440.0], seed=6)
    y1 = engine.process(x1)
    assert y1.shape == (1, x1.shape[1])
    assert max_abs_error(y1[0], reference_convolution(x1[0], h)) < 1e-12


def test_wrong_channel_count_rejected():
    engine = MultiChannelFIR(2, filters.identity())
    with pytest.raises(ValueError):
        engine.process(np.zeros((3, 10)))


# ----------------------------------------------------------------------
# 5. Boundary discontinuity detection
# ----------------------------------------------------------------------
def test_no_boundary_discontinuity_vs_reference():
    """Streaming output must be sample-identical to the whole-signal
    reference, so block boundaries introduce no detectable jump."""
    rng = np.random.default_rng(13)
    x = rng.standard_normal(8192)
    h = filters.lowpass(127, 3000.0, 48000.0)
    block = 512
    engine = StreamingFIR(h)
    y = stream_in_blocks(engine, x, block)
    ref = reference_convolution(x, h)
    err = max_abs_error(y, ref)
    assert err < 1e-10
    # Boundary jumps of the streamed signal match the reference's jumps.
    ref_jumps = boundary_jumps(ref, block)
    y_jumps = boundary_jumps(y, block)
    assert max_abs_error(y_jumps, ref_jumps) < 1e-10


def test_discontinuity_detector_finds_planted_jump():
    y = np.zeros(1000)
    y[500:] = 1.0  # step of 1.0 at index 500
    idx = find_discontinuities(y, threshold=0.5)
    assert idx.tolist() == [500]
    # A smooth sine has no discontinuities at the same threshold.
    t = np.arange(1000) / 48000.0
    assert find_discontinuities(np.sin(2 * np.pi * 440 * t), 0.5).size == 0


# ----------------------------------------------------------------------
# 6. Offline service end-to-end (synth + PCM file inputs)
# ----------------------------------------------------------------------
def test_service_synth_job_end_to_end(tmp_path):
    job = {
        "sample_rate": 48000,
        "channels": 2,
        "block_size": 256,
        "input": {"type": "synth", "duration_s": 0.5, "freqs_hz": [440, 5000], "seed": 1},
        "filter": {"type": "lowpass", "num_taps": 63, "cutoff_hz": 1000},
        "schedule": [
            {
                "at_sample": 12000,
                "filter": {"type": "highpass", "num_taps": 63, "cutoff_hz": 3000},
                "fade_samples": 512,
            }
        ],
        "output": {"path": "out.f32", "format": "f32le", "report_path": "report.json"},
    }
    report = service.run_job(job, base_dir=str(tmp_path))
    assert report["samples_out"] == report["samples_in"] == 24000
    assert report["num_blocks"] > 0
    assert len(report["switches_applied"]) == 1
    assert report["max_boundary_jump"] < 1.0  # smooth crossfade, no clicks

    out = pcm.read_pcm(str(tmp_path / "out.f32"), "f32le", 2)
    assert out.shape == (2, 24000)
    saved = json.loads((tmp_path / "report.json").read_text())
    assert saved["samples_out"] == 24000


def test_service_pcm_roundtrip_and_switch(tmp_path):
    sr = 48000
    x = synth.multi_sine(2, 0.25, sr, [300.0, 6000.0], seed=21)
    pcm.write_pcm(str(tmp_path / "in.pcm"), x, "s16le")

    job = {
        "sample_rate": sr,
        "channels": 2,
        "block_size": 128,
        "input": {"type": "pcm", "path": "in.pcm", "format": "s16le"},
        "filter": {"type": "identity"},
        "schedule": [
            {"at_sample": 6000, "filter": {"type": "moving_average", "num_taps": 9},
             "fade_samples": 256, "channel": 0}
        ],
        "output": {"path": "out.pcm", "format": "s16le", "report_path": "report.json"},
    }
    report = service.run_job(job, base_dir=str(tmp_path))
    assert report["samples_out"] == x.shape[1]
    out = pcm.read_pcm(str(tmp_path / "out.pcm"), "s16le", 2)

    # Channel 1 (untouched, identity filter) round-trips through s16.
    xq = pcm.read_pcm(str(tmp_path / "in.pcm"), "s16le", 2)
    assert max_abs_error(out[1], xq[1]) < 1e-4
    # Channel 0 settles on the moving average after the fade.
    ref = reference_convolution(xq[0], filters.moving_average(9))
    assert max_abs_error(out[0][6256 + 32 :], ref[6256 + 32 :]) < 1e-3


def test_service_rejects_bad_schedule(tmp_path):
    job = {
        "sample_rate": 48000,
        "channels": 1,
        "block_size": 64,
        "input": {"type": "synth", "duration_s": 0.01, "freqs_hz": [440], "seed": 0},
        "filter": {"type": "identity"},
        "schedule": [{"at_sample": 999999, "filter": {"type": "identity"}}],
        "output": {"path": "o.f32", "format": "f32le"},
    }
    with pytest.raises(ValueError):
        service.run_job(job, base_dir=str(tmp_path))


def test_cli_runs_job_file(tmp_path, capsys):
    from fir_stream.cli import main

    job = {
        "sample_rate": 48000,
        "channels": 1,
        "block_size": 100,
        "input": {"type": "synth", "duration_s": 0.02, "freqs_hz": [440], "seed": 0},
        "filter": {"type": "delay", "samples": 10},
        "schedule": [],
        "output": {"path": "cli_out.f32", "format": "f32le"},
    }
    job_path = tmp_path / "job.json"
    job_path.write_text(json.dumps(job))
    assert main([str(job_path)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["samples_out"] == 960
    assert (tmp_path / "cli_out.f32").exists()
