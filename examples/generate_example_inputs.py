#!/usr/bin/env python3
"""Generate example inputs for the EKF fusion service.

The ground truth is uniform motion along (1, 0.4) m/s starting at the origin.
Two streams are produced at different frequencies and phases:

* odometry (velocity) every 0.10 s, phase 0.02 s
* GNSS     (position) every 0.20 s, phase 0.10 s

The arrival list is deliberately shuffled with a *bounded* displacement so no
message lags the high-water mark by more than ~1.4 s (inside the 2 s replay
window), i.e. the service must reorder and replay rather than fuse in arrival
order.  Two extra entries demonstrate evidence-based rejection:

* one gross GNSS blip at t = 5.10 s (a ~50 m jump -> NIS gate)
* one structurally invalid measurement covariance (asymmetric)
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

import numpy as np

V_TRUE = np.array([1.0, 0.4])
P0 = np.array([0.0, 0.0])
RNG_SEED = 20260923

# bounded displacement: element originally at i arrives in a slot in
# [i-MAX_LAG, i+MAX_LAG] (K-bounded permutation, constructed exactly by the
# empty-slot algorithm below)
MAX_LAG = 14


def build() -> dict:
    rng = random.Random(RNG_SEED)
    nprng = np.random.default_rng(RNG_SEED)
    messages = []

    odom_t = np.arange(0.02, 10.0, 0.10)
    gnss_t = np.arange(0.10, 10.0, 0.20)

    mid_i = 0

    def add(t, kind, z, r):
        nonlocal mid_i
        messages.append({
            "message_id": f"m{mid_i:04d}",
            "t": round(float(t), 6),
            "kind": kind,
            "z": [round(float(v), 6) for v in z],
            "R": r,
        })
        mid_i += 1

    for t in odom_t:
        z = V_TRUE + nprng.normal(0.0, 0.03, size=2)
        r = [[0.0009, 0.0], [0.0, 0.0009]]
        add(t, "odom", z, r)

    for i, t in enumerate(gnss_t):
        pos = P0 + V_TRUE * t + nprng.normal(0.0, 0.15, size=2)
        r = [[0.0225, 0.0], [0.0, 0.0225]]
        add(t, "gnss", pos, r)

    # sort by measurement time first, so every id maps to a known timeline slot
    messages.sort(key=lambda m: (m["t"], 0 if m["kind"] == "odom" else 1))

    # gross GNSS blip near t=5.1 (already in the gnss list as a normal point):
    # mark the normal one and append an extra blip-style record is NOT done;
    # instead corrupt the existing GNSS point at that time.
    for m in messages:
        if m["kind"] == "gnss" and abs(m["t"] - 5.1) < 1e-9:
            m["z"] = [m["z"][0] + 50.0, m["z"][1] - 40.0]
            m["note"] = "gross outlier: expect NIS-gate rejection"
            break

    # bounded-lateness arrival permutation (deterministic K-bounded shuffle).
    # Element i may occupy slot s only if |s-i| <= MAX_LAG.  At slot s the
    # only elements that can be placed are the not-yet-used ones with i <=
    # s+k; element (s-k) is at its deadline and is forced.  This is the
    # standard feasible greedy for unit jobs with release/deadline windows.
    n = len(messages)
    k = MAX_LAG
    used = [False] * n
    arrival: list[dict] = []
    for s in range(n):
        forced = s - k
        if forced >= 0 and not used[forced]:
            choice = forced
        else:
            upper = min(n - 1, s + k)
            candidates = [i for i in range(0, upper + 1) if not used[i]]
            choice = rng.choice(candidates)
        used[choice] = True
        arrival.append(messages[choice])
    assert len(arrival) == n

    # verify the permutation and its displacement bound
    seen = {a["message_id"] for a in arrival}
    assert len(seen) == n
    for s, a in enumerate(arrival):
        orig = next(i for i, m in enumerate(messages) if m["message_id"] == a["message_id"])
        assert abs(s - orig) <= k, (s, orig, k)

    # structurally bad covariance: asymmetric R -> rejected before fusion
    bad = {
        "message_id": "m-bad-cov",
        "t": 9.95,
        "kind": "gnss",
        "z": [9.95, 3.98],
        "R": [[0.05, 0.02], [0.09, 0.05]],  # R[0,1] != R[1,0]
        "note": "asymmetric covariance: expect covariance_not_symmetric",
    }
    arrival.append(bad)

    return {
        "description": (
            "Bounded-late shuffled odometry (10 Hz) + GNSS (5 Hz) "
            "measurements for uniform motion v=(1.0, 0.4) m/s; one gross "
            "GNSS outlier and one invalid covariance are included."),
        "truth": {"p0": P0.tolist(), "v": V_TRUE.tolist()},
        "late_window_s": 2.0,
        "max_arrival_lag_slots": MAX_LAG,
        "measurements": arrival,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).with_name("measurements.json")))
    args = ap.parse_args()
    data = build()
    Path(args.out).write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"wrote {len(data['measurements'])} measurements -> {args.out}")


if __name__ == "__main__":
    main()
