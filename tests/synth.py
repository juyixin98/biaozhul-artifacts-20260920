"""Synthetic IMU data builders for tests."""

from __future__ import annotations

import numpy as np

G = 9.80665


def gravity_body(tilt_deg: float = 15.0) -> np.ndarray:
    c, s = np.cos(np.deg2rad(tilt_deg)), np.sin(np.deg2rad(tilt_deg))
    return np.array([G * c, 0.0, G * s])


def make_static(
    duration=10.0,
    fs=100.0,
    bias=(0.01, -0.005, 0.002),
    tilt_deg=15.0,
    gyro_noise=2e-3,
    accel_noise=0.01,
    seed=0,
    t0=0.0,
):
    rng = np.random.default_rng(seed)
    n = int(round(duration * fs))
    t = t0 + np.arange(n) / fs
    g = np.tile(bias, (n, 1)) + rng.normal(0.0, gyro_noise, (n, 3))
    a = np.tile(gravity_body(tilt_deg), (n, 1)) + rng.normal(
        0.0, accel_noise, (n, 3)
    )
    return t, a, g


def add_motion(t, a, g, start, end, seed=1):
    """Sudden sustained motion on [start, end)."""
    rng = np.random.default_rng(seed)
    m = (t >= start) & (t < end)
    g[m] += np.column_stack(
        [
            0.7 * np.sin(2 * np.pi * 0.7 * t[m]),
            0.4 * np.sin(2 * np.pi * 0.9 * t[m]),
            0.5 * np.cos(2 * np.pi * 0.4 * t[m]),
        ]
    )
    a[m] += rng.normal(0.0, 1.5, (m.sum(), 3))
    a[m, 0] += 1.2 * np.sin(2 * np.pi * 0.6 * t[m])
    return m
