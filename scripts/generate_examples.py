#!/usr/bin/env python3
"""Generate realistic example request/response batches.

The simulator does *not* assume symmetric one-way delay: ``downlink_s`` and
``uplink_s`` are independent (and deliberately different).  Host/device clocks
follow::

    device_counter = (host_t - t0) / tick_s

with configurable wrap modulus, restart, host time jump and drift-rate change.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


def simulate(
    kind: str,
    *,
    n: int = 60,
    tick_s: float = 1e-3,        # device advances 1 tick per millisecond
    downlink_s: float = 0.004,
    uplink_s: float = 0.012,     # asymmetric: uplink 3x slower
    jitter_s: float = 0.0008,
    modulus: float | None = 10_000.0,
    sim_modulus: float | None = None,
    seed: int = 7,
):
    """``modulus`` is what we tell the API; ``sim_modulus`` is what the
    simulated hardware actually wraps at (they may differ, e.g. unknown)."""

    rng = np.random.default_rng(seed)
    if sim_modulus is None:
        sim_modulus = modulus
    t0 = 1_700_000_000.0
    samples = []
    epoch_offset = 0.0  # accumulated host-side shift (time jump)
    restart_after = None
    drift_kink_i = n // 2

    times = t0 + np.arange(n, dtype=float) * 0.5  # a request every 0.5 s

    for i, h in enumerate(times):
        if kind == "time_jump" and i == n // 2:
            epoch_offset += 3.0  # host clock jumps forward 3 s mid-run
        if kind == "restart" and i == n // 2:
            restart_after = i

        host_send = h + epoch_offset
        d = downlink_s + abs(rng.normal(0, jitter_s))
        u = uplink_s + abs(rng.normal(0, jitter_s))
        # device counter at the moment it stamps the request (after downlink)
        stamp_time = host_send + d
        if kind == "drift_change":
            # continuous piecewise-linear counter: rate kink, no offset jump
            elapsed = stamp_time - t0
            elapsed_kink = times[drift_kink_i] - t0
            if elapsed < elapsed_kink:
                raw = elapsed / tick_s
            else:
                raw = (elapsed_kink / tick_s) + (
                    elapsed - elapsed_kink
                ) / (tick_s / 1.5)
        else:
            raw = (stamp_time - t0 - epoch_offset) / tick_s
        if restart_after is not None and i >= restart_after:
            # counter restarts near zero
            raw = (stamp_time - times[restart_after]) / tick_s
        if sim_modulus is not None and kind != "restart":
            raw %= sim_modulus
        host_recv = stamp_time + u
        samples.append(
            {
                "t_send": round(float(host_send), 6),
                "t_recv": round(float(host_recv), 6),
                "counter": float(raw),
            }
        )
        if kind == "rtt_spikes" and i in (12, 33, 47):
            samples[-1]["t_recv"] += 0.6  # congestion spikes

    req = {
        "device_id": f"demo-{kind}",
        "modulus": modulus if kind != "unknown_modulus" else None,
        "samples": samples,
    }
    return req


KINDS = {
    # asymmetric delay only; clean wrap inside modulus
    "asymmetric": "asymmetric delay (down 4 ms / up 12 ms), counter wraps 3x",
    "rtt_spikes": "asymmetric delay + three 600 ms RTT spikes to be rejected",
    "time_jump": "host/device clock jumps +3 s halfway through",
    "drift_change": "device drift rate halves halfway through",
    "wrap": "small modulus, many clean counter wraps",
    "restart": "device reboots; counter restarts near zero (modulus given)",
    "few": "only four samples (evidence must be reported insufficient)",
    "unknown_modulus": "counter moves backwards but modulus is unknown",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="examples")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    plan = {
        "asymmetric": dict(n=60, modulus=10_000.0),
        "rtt_spikes": dict(n=60, modulus=10_000.0),
        "time_jump": dict(n=60, modulus=100_000.0),
        "drift_change": dict(n=80, modulus=100_000.0),
        "wrap": dict(n=80, modulus=6_000.0),
        "restart": dict(n=60, modulus=100_000.0),
        "few": dict(n=8, modulus=10_000.0),
        "unknown_modulus": dict(
            n=60, modulus=None, sim_modulus=10_000.0
        ),
    }
    for name, kw in plan.items():
        req = simulate(name, **kw)
        p = out / f"{name}.json"
        p.write_text(json.dumps(req, indent=2))
        print(f"wrote {p}  -- {KINDS[name]}")


if __name__ == "__main__":
    main()
