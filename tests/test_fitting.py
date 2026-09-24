"""Tests for the interval-regression fitting core.

These encode the key honesty property: the service returns *error bounds* and
must never assume the network delay is symmetric.
"""

from __future__ import annotations

import numpy as np

from app.fitting import (
    fit_segment,
    prediction_bounds,
    rtt_filter,
)
from tests.sim import make_observations


def test_rtt_filter_drops_spikes_keeps_body():
    obs = make_observations(
        n=60, rtt_spikes=(12, 33, 47), spike_s=0.6, seed=3
    )
    ts = np.array([o.t_send for o in obs])
    tr = np.array([o.t_recv for o in obs])
    r = rtt_filter(ts, tr)
    assert set(r.dropped_idx.tolist()) == {12, 33, 47}
    assert r.keep.sum() == 57


def test_rtt_filter_keeps_overwhelming_body_on_clean_batch():
    obs = make_observations(n=200, seed=11)
    ts = np.array([o.t_send for o in obs])
    tr = np.array([o.t_recv for o in obs])
    r = rtt_filter(ts, tr)
    # half-normal jitter produces a few tail values, but the filter must keep
    # the large majority of a congestion-free batch
    assert r.keep.mean() > 0.9
    # and never drop a small RTT (tight timing is information, not an outlier)
    shortest = int(np.argmin(tr - ts))
    assert r.keep[shortest]


def test_interval_covers_true_time_under_asymmetric_delay():
    rng = np.random.default_rng(42)
    alpha_true, beta_true = 1e9, 1e-3
    for trial in range(10):
        n = 40
        lo, hi, cc = [], [], []
        for i in range(n):
            send = alpha_true + i * 0.5
            d = 0.001 + abs(rng.normal(0, 5e-4))   # 1 ms down
            u = 0.100 + abs(rng.normal(0, 5e-3))   # 100 ms up
            stamp = send + d
            lo.append(send)
            hi.append(stamp + u)
            cc.append((stamp - alpha_true) / beta_true)
        f = fit_segment(np.array(cc), np.array(lo), np.array(hi))
        assert f.feasible
        assert f.beta_lo <= beta_true <= f.beta_hi
        for x in (cc[5], cc[20], cc[-5]):
            true_h = alpha_true + beta_true * x
            _, l, h = f.predict(x)
            assert l - 1e-9 <= true_h <= h + 1e-9


def test_offset_bias_from_asymmetry_is_bounded_not_hidden():
    # Constant asymmetric delay biases the midpoint estimator; the point
    # estimate may be off, but the interval must still contain the truth.
    obs = make_observations(
        n=60, downlink_s=0.002, uplink_s=0.05, seed=5
    )
    c = np.array([o.counter for o in obs])
    ts = np.array([o.t_send for o in obs])
    tr = np.array([o.t_recv for o in obs])
    f = fit_segment(c, ts, tr)
    assert f.feasible
    # the 26 ms asymmetry must show up as an interval at least ~24 ms wide
    assert f.offset_halfwidth() * 2 >= 0.024


def test_infeasible_when_no_counter_span():
    obs = make_observations(n=10, seed=1)
    c = np.zeros(10)
    ts = np.array([o.t_send for o in obs])
    tr = np.array([o.t_recv for o in obs])
    f = fit_segment(c, ts, tr)
    assert not f.feasible
    assert "span" in f.reasons[0]


def test_trim_rescues_one_bad_interval():
    obs = make_observations(n=40, seed=2)
    c = np.array([o.counter for o in obs])
    ts = np.array([o.t_send for o in obs])
    tr = np.array([o.t_recv for o in obs])
    tr[20] += 5.0  # one wild delayed response
    f = fit_segment(c, ts, tr)
    assert f.feasible
    assert 20 in f.trimmed_idx
    assert f.n_used == 39


def test_prediction_bounds_expand_outside_data():
    obs = make_observations(n=40, seed=9)
    c = np.array([o.counter for o in obs])
    ts = np.array([o.t_send for o in obs])
    tr = np.array([o.t_recv for o in obs])
    f = fit_segment(c, ts, tr)
    _, l_in, h_in = f.predict(c[20])
    _, l_out, h_out = f.predict(c[-1] * 1.5)
    assert (h_out - l_out) > (h_in - l_in)


def test_prediction_bounds_exact_envelope_math():
    # tiny hand-checked example, symmetric-ish intervals
    c = np.array([0.0, 10.0, 20.0])
    lo = np.array([0.0, 1.0, 2.0])
    hi = np.array([0.1, 1.1, 2.1])
    blo, bhi, ok = (None, None, None)
    from app.fitting import _beta_range

    blo, bhi, ok = _beta_range(c, lo, hi)
    assert ok
    # beta=0.1 satisfies all; bounds bracket it
    assert blo <= 0.1 <= bhi
    l, h = prediction_bounds(0.0, c, lo, hi, blo, bhi)
    assert l <= 0.05 <= h
