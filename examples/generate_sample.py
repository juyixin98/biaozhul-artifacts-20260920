"""Generate the example request payload at examples/sample_request.json.

Scenario (100 Hz, 60 s): warm-up stationary 0-10 s with temperature ramping
and a mild gyro bias temperature slope, sudden motion 10-25 s, static again
25-60 s at a higher temperature — plus one injected gyro spike and one
duplicate timestamp. Demonstrates every branch of the detector.
"""

from __future__ import annotations

import argparse
import json
import os

import numpy as np


def generate(seed: int = 7) -> dict:
    rng = np.random.default_rng(seed)
    fs = 100.0
    t = np.arange(0.0, 60.0, 1.0 / fs)

    # temperature: 25 C -> 42 C warm-up over 60 s
    temp = 25.0 + 17.0 * (1.0 - np.exp(-t / 20.0))

    # gyro bias with temperature slope: 0.002 rad/s per deg C on x,
    # 0.001 on y, none on z; plus run-up 5e-5 rad/s per second early on
    b_g = np.column_stack(
        [
            0.01 + 0.002 * (temp - 25.0),
            -0.005 + 0.001 * (temp - 25.0),
            0.002 + np.zeros_like(t),
        ]
    )

    g = b_g + rng.normal(0.0, 2e-3, size=(t.size, 3))

    # static gravity vector (body tilted ~15 deg about y)
    g0 = 9.80665
    c, s = np.cos(np.deg2rad(15.0)), np.sin(np.deg2rad(15.0))
    a_static = np.tile([g0 * c, 0.0, g0 * s], (t.size, 1))
    a = a_static + rng.normal(0.0, 0.01, size=(t.size, 3))

    # sudden motion between 10 s and 25 s
    motion = (t >= 10.0) & (t < 25.0)
    g[motion] += np.column_stack(
        [
            0.6 * np.sin(2.0 * np.pi * 0.5 * t[motion]),
            0.3 * np.sin(2.0 * np.pi * 0.8 * t[motion]),
            0.4 * np.cos(2.0 * np.pi * 0.3 * t[motion]),
        ]
    )
    a[motion] += rng.normal(0.0, 1.2, size=(motion.sum(), 3))
    a[motion] += np.column_stack(
        [
            0.8 * np.sin(2.0 * np.pi * 0.5 * t[motion]),
            np.zeros(motion.sum()),
            0.5 * np.cos(2.0 * np.pi * 0.6 * t[motion]),
        ]
    )

    # time gap: drop samples between 30 s and 32.5 s
    keep = ~((t > 30.0) & (t < 32.5))
    t, g, a, temp = t[keep], g[keep], a[keep], temp[keep]

    # single anomalous gyro spike at ~40 s
    spike_i = int(np.argmin(np.abs(t - 40.0)))
    g[spike_i, 1] += 4.5

    # duplicate timestamp at ~45 s (first policy by default)
    dup_i = int(np.argmin(np.abs(t - 45.0)))
    t = np.insert(t, dup_i + 1, t[dup_i])
    g = np.insert(g, dup_i + 1, g[dup_i] + [0.001, -0.002, 0.0], axis=0)
    a = np.insert(a, dup_i + 1, a[dup_i] + [0.01, 0.0, -0.01], axis=0)
    temp = np.insert(temp, dup_i + 1, temp[dup_i])

    return {
        "timestamps": [round(float(v), 4) for v in t],
        "accelerometer": [[round(float(x), 6) for x in row] for row in a],
        "gyroscope": [[round(float(x), 7) for x in row] for row in g],
        "temperature": [round(float(v), 4) for v in temp],
        "units": {
            "time": "s",
            "acceleration": "m/s^2",
            "angular_velocity": "rad/s",
            "temperature": "c",
        },
        "axes": {
            "accelerometer": ["x", "y", "z"],
            "gyroscope": ["x", "y", "z"],
        },
        "config": {},
        "time_repeat_policy": "first",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        default=os.path.join(os.path.dirname(__file__), "sample_request.json"),
    )
    args = parser.parse_args()
    payload = generate()
    with open(args.out, "w") as f:
        json.dump(payload, f, indent=1)
    print(f"wrote {args.out} with {len(payload['timestamps'])} samples")


if __name__ == "__main__":
    main()
