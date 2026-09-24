"""Unit tests for the fitting math: drift, asymmetry, filtering, segments."""

from __future__ import annotations

import math

import numpy as np
import pytest

from app.fitting import (
    MIN_POINTS_CALIBRATED,
    Observation,
    calibrate,
    fit_segment,
    predict_interval,
    rtt_filter,
    segment_observations,
)
from tests.synth import make_samples, with_rtt_spikes

HZ = 1_000_000.0
RNG = np.random.default_rng(42)


# --------------------------------------------------------------------------
# Baseline drift recovery
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ppm", [-350.0, 0.0, 217.0])
def test_recovers_known_drift(ppm):
    obs = make_samples(n=30, ppm=ppm, up_delay=0.01, down_delay=0.01)
    seg_outs, events, _ = calibrate(obs, None, RNG)
    assert len(seg_outs) == 1 and not events
    f = seg_outs[0].fit
    assert f.status == "calibrated"
    true_slope = 1.0 / ((1.0 + ppm * 1e-6) * HZ)
    assert f.slope_ci is not None
    assert f.slope_ci[0] <= true_slope <= f.slope_ci[1]
    assert abs(f.slope - true_slope) / true_slope < 5e-4


def test_points_and_predictions_cover_truth():
    ppm = 120.0
    obs = make_samples(n=30, ppm=ppm, up_delay=0.03, down_delay=0.005)
    seg_outs, _, _ = calibrate(obs, None, RNG)
    f = seg_outs[0].fit
    # For every observed counter, the true host time must lie inside [lo, hi].
    for o in obs:
        true_t = 10_000.0 + o.c_recv / ((1.0 + ppm * 1e-6) * HZ)
        _, lo, hi = predict_interval(f, o.c_recv)
        assert lo - 1e-9 <= true_t <= hi + 1e-9


# --------------------------------------------------------------------------
# Asymmetric delays — error bounds instead of symmetry assumption
# --------------------------------------------------------------------------


def test_asymmetric_delay_bounds_are_asymmetric_and_valid():
    """Slow uplink: truth sits toward the late side of each window.

    The estimator must widen the interval and keep the truth bracketed;
    a symmetric-delay assumption would be systematically biased.
    """
    ppm = 0.0
    obs = make_samples(n=40, ppm=ppm, up_delay=0.08, down_delay=0.002,
                       jitter_frac=0.1)
    seg_outs, _, _ = calibrate(obs, None, RNG)
    f = seg_outs[0].fit
    assert f.status == "calibrated"
    violations = 0
    for o in obs[::3]:
        true_t = 10_000.0 + o.c_recv / HZ
        point, lo, hi = predict_interval(f, o.c_recv)
        if not (lo - 1e-9 <= true_t <= hi + 1e-9):
            violations += 1
    assert violations == 0
    # Interval must reflect the heavy asymmetry: tens of milliseconds wide.
    c_mid = 0.5 * (f.c_min + f.c_max)
    _, lo, hi = predict_interval(f, c_mid)
    assert (hi - lo) > 0.02


def test_interval_never_degenerate_under_jitter():
    obs = make_samples(n=24, ppm=50.0, up_delay=0.02, down_delay=0.02,
                       jitter_frac=0.5)
    seg_outs, _, _ = calibrate(obs, None, RNG)
    f = seg_outs[0].fit
    _, lo, hi = predict_interval(f, 0.5 * (f.c_min + f.c_max))
    assert hi > lo


# --------------------------------------------------------------------------
# RTT outlier filtering
# --------------------------------------------------------------------------


def test_rtt_spikes_are_filtered():
    good = make_samples(n=24, ppm=0.0, up_delay=0.005, down_delay=0.005)
    obs = with_rtt_spikes(good, [3, 11, 19], extra_rtt=3.0)
    keep, med, thr = rtt_filter(obs)
    for i in (3, 11, 19):
        assert not keep[i]
    assert sum(keep) >= MIN_POINTS_CALIBRATED - 2
    # Filtered fit should be much closer to truth than unfiltered.
    f = fit_segment(obs, [0.0] * len(obs), RNG, rtt_keep=keep)
    assert f.n_rtt_filtered >= 3
    true_slope = 1.0 / HZ
    assert abs(f.slope - true_slope) / true_slope < 1e-3


def test_tiny_samples_not_filtered():
    obs = make_samples(n=4)
    keep, _, _ = rtt_filter(obs)
    assert all(keep)


# --------------------------------------------------------------------------
# Few samples -> uncertain
# --------------------------------------------------------------------------


def test_too_few_samples_is_uncertain():
    obs = make_samples(n=3, ppm=0.0)
    seg_outs, _, _ = calibrate(obs, None, RNG)
    assert seg_outs[0].fit.status == "uncertain"
    assert seg_outs[0].fit.reason


def test_short_span_vs_rtt_is_uncertain():
    # 8 samples microseconds apart while RTT is tens of ms: span < 2*RTT.
    obs = make_samples(n=8, ppm=0.0, spacing=0.0005,
                       up_delay=0.02, down_delay=0.02)
    seg_outs, _, _ = calibrate(obs, None, RNG)
    assert seg_outs[0].fit.status == "uncertain"


# --------------------------------------------------------------------------
# Wrap vs reboot vs time jump
# --------------------------------------------------------------------------


def test_counter_wrap_does_not_split_regime():
    M = float(2 ** 16)
    obs = make_samples(n=40, ppm=100.0, spacing=0.02, modulus=M,
                       up_delay=0.001, down_delay=0.001, processing=0.0002)
    segments, events = segment_observations(obs, M, 1e6)
    wraps = [e for e in events if e.type == "wrap"]
    assert wraps, "at least one wrap event must be detected"
    assert len(segments) == 1, "a pure wrap must keep the clock regime intact"
    # Unwrapped counters must be strictly non-decreasing.
    unwrapped = [o.c_recv + u for o, u in zip(segments[0].obs,
                                              segments[0].offsets)]
    assert all(b >= a - 1e-9 for a, b in zip(unwrapped, unwrapped[1:]))


def test_reboot_is_detected_and_segments_split():
    obs = make_samples(n=30, ppm=0.0, reboot_at=15)
    segments, events = segment_observations(obs, None)
    kinds = {e.type for e in events}
    assert "reboot" in kinds
    assert len(segments) == 2
    # Offsets restart independently after a reboot.
    assert segments[1].offsets[0] == 0.0


def test_reboot_with_modulus_distinguished_from_wrap():
    # Reset near zero but arriving *not* from the top of the range cannot be
    # a rollover.
    obs = make_samples(n=24, ppm=0.0, spacing=1.0)
    # Force a reset at index 12 while counters are still tiny vs M.
    M = float(2 ** 32)
    # Manually rebuild: pre-reset counters stay < 5% of M.
    obs = make_samples(n=12, ppm=0.0, spacing=0.0001,
                       up_delay=0.0005, down_delay=0.0005)
    post = make_samples(n=12, ppm=0.0, spacing=0.0001,
                        t_start=obs[-1].t0 + 0.5,
                        up_delay=0.0005, down_delay=0.0005, seed=9)
    all_obs = obs + post
    segments, events = segment_observations(all_obs, M, 1e6)
    assert any(e.type in ("reboot", "uncertain") for e in events)
    assert not any(e.type == "wrap" for e in events)


def test_ambiguous_backward_jump_is_uncertain():
    # Two samples only before a backward jump: no rate baseline available.
    a = Observation(0.0, 0.02, 1000.0, 1001.0, 0)
    b = Observation(1.0, 1.02, 500.0, 501.0, 1)
    segments, events = segment_observations([a, b], modulus=None)
    assert events and events[0].type == "uncertain"
    assert len(segments) == 2


def test_forward_time_jump_detected():
    obs = make_samples(n=10, ppm=0.0, up_delay=0.002, down_delay=0.002)
    # Inject a 1000-second counter leap on one sample.
    leap = obs[5]
    obs[5] = Observation(leap.t0, leap.t3, leap.c_recv + 1000.0 * HZ,
                         leap.c_send + 1000.0 * HZ if leap.c_send else None,
                         leap.seq)
    segments, events = segment_observations(obs, None)
    assert any(e.type == "time_jump" for e in events)


# --------------------------------------------------------------------------
# Drift change
# --------------------------------------------------------------------------


def test_drift_changepoint_detected():
    # Continuous counter whose rate changes at t_drift — no counter reset,
    # so this is a genuine oscillator drift change, not a reboot.
    ppm1, ppm2 = 400.0, -400.0
    t_start, spacing = 10_000.0, 1.0
    n1, n2 = 18, 18
    t_drift = t_start + n1 * spacing
    c_at_drift = (1 + ppm1 * 1e-6) * HZ * (t_drift - t_start)
    obs = []
    for i in range(n1 + n2):
        t0 = t_start + i * spacing
        if t0 < t_drift:
            ctr = (1 + ppm1 * 1e-6) * HZ * (t0 - t_start)
        else:
            ctr = c_at_drift + (1 + ppm2 * 1e-6) * HZ * (t0 - t_drift)
        obs.append(Observation(t0, t0 + 0.004, ctr, ctr + 0.002 * HZ, i))
    seg_outs, events, _ = calibrate(obs, None, RNG)
    assert any(e.type == "drift_change" for e in events)
    slopes = [so.fit.slope for so in seg_outs if so.fit.slope is not None]
    assert len(slopes) >= 2
    # Segments should have opposite-sign ppm deviations.
    assert slopes[0] != pytest.approx(slopes[-1], rel=1e-4)
    # Recovered slopes track the true rates.
    assert abs(slopes[0] - 1.0 / ((1 + ppm1 * 1e-6) * HZ)) / slopes[0] < 1e-3
    assert abs(slopes[-1] - 1.0 / ((1 + ppm2 * 1e-6) * HZ)) / abs(slopes[-1]) < 1e-3


def test_stable_drift_not_reported_as_changepoint():
    obs = make_samples(n=40, ppm=150.0, up_delay=0.002, down_delay=0.002,
                       jitter_frac=0.15)
    seg_outs, events, _ = calibrate(obs, None, RNG)
    assert not any(e.type == "drift_change" for e in events)


def test_unresolvable_drift_break_is_not_guessed():
    """A 500 ppm slope break buried under RTT noise on a short span must NOT
    be reported: the changepoint rule requires statistical separation."""
    rng = np.random.default_rng(7)
    hz = HZ
    n, space, rtt = 18, 0.034, 0.008
    t_start = 9000.0
    t_drift = t_start + n * space
    c_drift = (1 + 200e-6) * hz * (t_drift - t_start)
    obs = []
    for i in range(2 * n):
        t0 = t_start + i * space
        up = rtt * 0.5 * (1 + rng.uniform(-0.2, 0.2))
        down = rtt * 0.5 * (1 + rng.uniform(-0.2, 0.2))
        if t0 < t_drift:
            c = (1 + 200e-6) * hz * (t0 + up - t_start)
            cs = (1 + 200e-6) * hz * (t0 + up + 0.001 - t_start)
        else:
            c = c_drift + (1 - 300e-6) * hz * (t0 + up - t_drift)
            cs = c_drift + (1 - 300e-6) * hz * (t0 + up + 0.001 - t_drift)
        obs.append(Observation(t0, t0 + up + 0.001 + down, c, cs, i))
    seg_outs, events, _ = calibrate(obs, None, RNG)
    assert not any(e.type == "drift_change" for e in events)
