"""Synthetic observation helpers shared by tests."""

from __future__ import annotations

import math
import random
from typing import Optional

from app.fitting import Observation


def make_samples(
    n: int = 30,
    *,
    ppm: float = 0.0,
    hz: float = 1_000_000.0,
    t_start: float = 10_000.0,
    spacing: float = 1.0,
    up_delay: float = 0.01,
    down_delay: float = 0.01,
    processing: float = 0.001,
    jitter_frac: float = 0.2,
    dual_stamp: bool = True,
    modulus: Optional[float] = None,
    reboot_at: Optional[int] = None,
    seed: int = 1,
) -> list[Observation]:
    """Generate causally-consistent request/response samples.

    Ground truth: device counter advances at (1+ppm*1e-6)*hz.

    For dual-stamped samples the observation carries:
      c_recv stamped at host time t0 + up_delay   (only lower bound host>=t0)
      c_send stamped at host time t3 - down_delay (only upper bound host<=t3)
    """
    rng = random.Random(seed)
    obs: list[Observation] = []
    # device counter state; reboot resets the epoch offset
    epoch_host = t_start
    epoch_counter = 0.0

    def counter_at(host_t: float) -> float:
        c = epoch_counter + (1.0 + ppm * 1e-6) * hz * (host_t - epoch_host)
        return c % modulus if modulus is not None else c

    rebooted = False
    for i in range(n):
        if reboot_at is not None and i == reboot_at:
            epoch_host = t_start + i * spacing
            epoch_counter = rng.uniform(0.0, hz * 0.001)  # near zero
            rebooted = True
        t0 = t_start + i * spacing
        up = up_delay * (1.0 + rng.uniform(-jitter_frac, jitter_frac))
        down = down_delay * (1.0 + rng.uniform(-jitter_frac, jitter_frac))
        up = max(0.0, up)
        down = max(0.0, down)
        proc = processing
        t_recv = t0 + up
        t_send = t_recv + proc
        t3 = t_send + down
        c_recv = counter_at(t_recv)
        c_send = counter_at(t_send) if dual_stamp else None
        obs.append(Observation(t0=t0, t3=t3, c_recv=c_recv, c_send=c_send, seq=i))
    return obs


def true_host_for_counter(c_unwrapped: float, *, ppm: float, hz: float,
                          t_start: float = 10_000.0) -> float:
    """Inversion of the ground-truth model used by make_samples."""
    return t_start + c_unwrapped / ((1.0 + ppm * 1e-6) * hz)


def with_rtt_spikes(obs: list[Observation], indices: list[int],
                    extra_rtt: float = 2.0) -> list[Observation]:
    out = []
    for i, o in enumerate(obs):
        if i in indices:
            out.append(Observation(o.t0, o.t3 + extra_rtt, o.c_recv,
                                   o.c_send, o.seq))
        else:
            out.append(o)
    return out
