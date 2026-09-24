"""Deterministic clock simulators for tests (asymmetric delay by design)."""

from __future__ import annotations

import numpy as np

from app.fitting import Observation


def make_observations(
    *,
    n=60,
    tick_s=1e-3,
    downlink_s=0.004,
    uplink_s=0.012,
    jitter_s=8e-4,
    modulus=None,
    seed=7,
    jump_at=None,
    jump_s=0.0,
    drift_kink=None,
    drift_ratio=1.0,
    restart_at=None,
    rtt_spikes=(),
    spike_s=0.6,
    t0=1_700_000_000.0,
    spacing_s=0.5,
):
    """Generate request/response samples on a device clock.

    One-way delays are deliberately asymmetric and independent, so any test
    that passes here cannot be relying on symmetric-delay assumptions.
    """

    rng = np.random.default_rng(seed)
    obs = []
    offset = 0.0
    counter_at_kink = None
    for i in range(n):
        if jump_at is not None and i == jump_at:
            offset += jump_s
        h = t0 + i * spacing_s + offset
        d = downlink_s + abs(rng.normal(0, jitter_s))
        u = uplink_s + abs(rng.normal(0, jitter_s))
        stamp = h + d

        if drift_kink is not None and drift_ratio != 1.0:
            t_kink = t0 + drift_kink * spacing_s
            if stamp - offset < t_kink:
                raw = (stamp - t0) / tick_s
            else:
                if counter_at_kink is None:
                    counter_at_kink = (t_kink - t0) / tick_s
                new_tick = tick_s / drift_ratio
                raw = counter_at_kink + (stamp - offset - t_kink) / new_tick
        else:
            raw = (stamp - t0 - offset) / tick_s

        if restart_at is not None and i >= restart_at:
            raw = (stamp - (t0 + restart_at * spacing_s)) / tick_s
        if modulus is not None and restart_at is None:
            raw %= modulus
        recv = stamp + u
        if i in rtt_spikes:
            recv += spike_s
        obs.append(Observation(float(h), float(recv), float(raw)))
    return obs
