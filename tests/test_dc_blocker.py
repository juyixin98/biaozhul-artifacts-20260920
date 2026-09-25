"""Acceptance tests for the streaming DC blocker.

Covers the required acceptance scenarios:
* biased sine          -> steady-state error and response time
* bias step            -> response time and steady-state error after the jump
* very short blocks    -> 1/2/3-sample blocks work and stay block-invariant
* block invariance     -> chunked processing is bit-identical to one-shot
* causality            -> no future-sample leakage (unlike block-mean removal)
* sample-rate re-set and state reset
"""

import math

import numpy as np
import pytest

from dc_blocker import DCBlocker
from dc_blocker.io import biased_sine, bias_step

FS = 48_000.0
CUTOFF = 5.0


def make_filter() -> DCBlocker:
    return DCBlocker(sample_rate=FS, cutoff_hz=CUTOFF)


# ----------------------------------------------------------------------
# biased sine: steady-state error + response time
# ----------------------------------------------------------------------
def test_biased_sine_steady_state_error_is_zero():
    n = int(5 * FS)
    x = biased_sine(n, FS, freq_hz=440.0, amplitude=0.5, bias=0.3)
    y = make_filter().process(x)
    tail = y[int(2 * FS) :]  # well past settling
    assert abs(float(np.mean(tail))) < 1e-3
    # sine itself must survive: RMS of the tail stays close to A/sqrt(2)
    assert float(np.sqrt(np.mean(tail**2))) == pytest.approx(0.5 / math.sqrt(2), rel=0.02)


def test_biased_sine_response_time_matches_model():
    x = biased_sine(int(3 * FS), FS, freq_hz=440.0, amplitude=0.0, bias=0.3)
    flt = make_filter()
    y = flt.process(x)
    tau = flt.time_constant_samples
    # after 1 tau the residual step response must be ~e^-1 of the bias
    idx = int(round(tau))
    assert abs(y[idx]) == pytest.approx(0.3 * math.exp(-1), rel=0.05)
    # after the 1% settling time the residual is below 1% of the bias
    settle = flt.settling_samples(0.01)
    assert np.all(np.abs(y[settle:]) < 0.01 * 0.3)


# ----------------------------------------------------------------------
# bias step: re-converges after the jump, no permanent error
# ----------------------------------------------------------------------
def test_bias_step_recovers_after_jump():
    n = int(4 * FS)
    step_at = n // 2
    x = bias_step(n, step_at, bias_before=0.2, bias_after=-0.4)
    flt = make_filter()
    y = flt.process(x)
    settle = flt.settling_samples(0.01)
    # settled before the jump ...
    assert np.all(np.abs(y[settle : step_at - 1]) < 0.01 * 0.2)
    # ... the jump appears as a -0.6 transient at the step index ...
    assert y[step_at] == pytest.approx(-0.6, abs=1e-3)
    # ... and decays back to zero with the model time constant
    tau = flt.time_constant_samples
    idx = step_at + int(round(tau))
    assert abs(y[idx]) == pytest.approx(0.6 * math.exp(-1), rel=0.05)
    assert np.all(np.abs(y[step_at + settle :]) < 0.01 * 0.6)


# ----------------------------------------------------------------------
# very short blocks
# ----------------------------------------------------------------------
@pytest.mark.parametrize("block_size", [1, 2, 3])
def test_very_short_blocks(block_size):
    x = biased_sine(257, FS, freq_hz=100.0, amplitude=0.4, bias=0.25)
    flt = make_filter()
    out = [flt.process(x[i : i + block_size]) for i in range(0, len(x), block_size)]
    y = np.concatenate(out)
    assert len(y) == len(x)
    assert np.all(np.isfinite(y))
    # same as one-shot, bit for bit
    np.testing.assert_array_equal(y, make_filter().process(x))


def test_empty_block_is_a_noop():
    flt = make_filter()
    y0 = flt.process(np.array([]))
    assert y0.shape == (0,)
    state_before = flt.state
    flt.process(np.array([]))
    assert flt.state == state_before


# ----------------------------------------------------------------------
# block invariance across many chunkings (incl. non-dividing sizes)
# ----------------------------------------------------------------------
@pytest.mark.parametrize("block_size", [1, 7, 100, 1024, 4096, 96_001])
def test_block_invariance(block_size):
    rng = np.random.default_rng(42)
    x = 0.3 + 0.5 * np.sin(2 * np.pi * 440 * np.arange(96_001) / FS) + 0.01 * rng.standard_normal(96_001)
    reference = make_filter().process(x)
    flt = make_filter()
    chunked = np.concatenate(
        [flt.process(x[i : i + block_size]) for i in range(0, len(x), block_size)]
    )
    np.testing.assert_array_equal(chunked, reference)


# ----------------------------------------------------------------------
# causality: no future leakage (the failure mode of block-mean removal)
# ----------------------------------------------------------------------
def test_no_future_leakage():
    x = np.zeros(1000)
    x[500:] = 1.0  # a step in the "future"
    y = make_filter().process(x)
    # output before the step must be exactly zero — it cannot know about it
    np.testing.assert_array_equal(y[:500], np.zeros(500))


# ----------------------------------------------------------------------
# sample-rate reconfiguration and reset
# ----------------------------------------------------------------------
def test_set_sample_rate_keeps_time_constant_in_seconds():
    flt = DCBlocker(sample_rate=48_000.0, cutoff_hz=5.0)
    tau_s = flt.time_constant_samples / flt.sample_rate
    flt.set_sample_rate(16_000.0)
    assert flt.sample_rate == 16_000.0
    assert flt.time_constant_samples / flt.sample_rate == pytest.approx(tau_s, rel=1e-12)
    assert 0.0 < flt.coefficient < 1.0


def test_set_sample_rate_rejects_nyquist_violation():
    flt = DCBlocker(sample_rate=48_000.0, cutoff_hz=5_000.0)
    with pytest.raises(ValueError):
        flt.set_sample_rate(8_000.0)  # cutoff 5 kHz >= Nyquist 4 kHz


def test_reset_clears_state():
    flt = make_filter()
    flt.process(np.full(100, 0.5))
    assert flt.state["y_prev"] != 0.0
    flt.reset()
    assert flt.state == {"x_prev": 0.0, "y_prev": 0.0}
    # after reset the filter behaves like a fresh instance
    x = biased_sine(1000, FS, bias=0.3)
    np.testing.assert_array_equal(flt.process(x), make_filter().process(x))


def test_input_is_not_mutated_and_output_is_new():
    x = np.full(64, 0.25)
    before = x.copy()
    y = make_filter().process(x)
    np.testing.assert_array_equal(x, before)
    assert y is not x and y.dtype == np.float64


# ----------------------------------------------------------------------
# validation
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "kwargs",
    [
        {"sample_rate": 0.0},
        {"sample_rate": -48_000.0},
        {"sample_rate": float("nan")},
        {"sample_rate": 48_000.0, "cutoff_hz": 0.0},
        {"sample_rate": 48_000.0, "cutoff_hz": -1.0},
        {"sample_rate": 48_000.0, "cutoff_hz": 24_000.0},  # at Nyquist
    ],
)
def test_invalid_configuration_rejected(kwargs):
    with pytest.raises(ValueError):
        DCBlocker(**kwargs)


def test_non_1d_block_rejected():
    with pytest.raises(ValueError):
        make_filter().process(np.zeros((4, 4)))
