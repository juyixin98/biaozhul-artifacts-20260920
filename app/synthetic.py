"""Synthetic point-cloud data for ICP testing and demos.

No real hardware is involved: ground-truth rigid transforms are generated
so registration error can be measured exactly.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class SyntheticScene:
    source: np.ndarray  # moving cloud, (N, 3)
    target: np.ndarray  # fixed cloud, (M, 3)
    R_true: np.ndarray  # (3, 3) ground-truth rotation source -> target
    t_true: np.ndarray  # (3,) ground-truth translation
    overlap: float
    noise_std: float
    kind: str


def random_rotation(rng: np.random.Generator, max_angle_deg: float | None = None) -> np.ndarray:
    """Uniform random SO(3) rotation, or one limited to ``max_angle_deg``."""
    if max_angle_deg is None:
        # Arvo's method: uniformly distributed rotation matrices.
        u1, u2, u3 = rng.random(3)
        q = np.array([
            np.sqrt(1.0 - u1) * np.sin(2.0 * np.pi * u2),
            np.sqrt(1.0 - u1) * np.cos(2.0 * np.pi * u2),
            np.sqrt(u1) * np.sin(2.0 * np.pi * u3),
            np.sqrt(u1) * np.cos(2.0 * np.pi * u3),
        ])
        x, y, z, w = q
        return np.array([
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ])

    axis = rng.normal(size=3)
    axis /= np.linalg.norm(axis)
    angle = np.deg2rad(max_angle_deg) * rng.random()
    x, y, z = axis
    c, s = np.cos(angle), np.sin(angle)
    C = 1.0 - c
    return np.array([
        [c + x * x * C, x * y * C - z * s, x * z * C + y * s],
        [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
        [z * x * C - y * s, z * y * C + x * s, c + z * z * C],
    ])


def rotation_about_axis(axis: np.ndarray, angle_deg: float) -> np.ndarray:
    axis = np.asarray(axis, dtype=float)
    axis = axis / np.linalg.norm(axis)
    x, y, z = axis
    a = np.deg2rad(angle_deg)
    c, s, C = np.cos(a), np.sin(a), 1.0 - np.cos(a)
    return np.array([
        [c + x * x * C, x * y * C - z * s, x * z * C + y * s],
        [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
        [z * x * C - y * s, z * y * C + x * s, c + z * z * C],
    ])


def _points(kind: str, n: int, rng: np.random.Generator) -> np.ndarray:
    if kind == "volume":
        return rng.uniform(-1.0, 1.0, size=(n, 3))
    if kind == "planar":
        p = rng.uniform(-1.0, 1.0, size=(n, 3))
        p[:, 2] = 0.0
        return p
    if kind == "collinear":
        s = rng.uniform(-1.0, 1.0, size=(n, 1))
        p = np.zeros((n, 3))
        p[:] = s * np.array([1.0, 0.3, 0.0])
        return p
    if kind == "clusters":
        # 3 well-separated asymmetric blobs: a favourable case with a basin
        # wider than a single diffuse cloud, and a clear wrong pose basin.
        centers = np.array([[0.0, 0.0, 0.0],
                            [2.5, 0.2, 0.1],
                            [0.3, 2.0, -0.4]])
        pts = []
        for i, ctr in enumerate(centers):
            k = n // 3 + (1 if i < n % 3 else 0)
            pts.append(ctr + rng.normal(scale=0.12, size=(k, 3)))
        return np.vstack(pts) - np.mean(np.vstack(pts), axis=0)
    raise ValueError(f"unknown point kind: {kind!r}")


def make_scene(
    n_points: int = 120,
    kind: str = "volume",
    angle_deg: float = 20.0,
    translation: float = 0.5,
    noise_std: float = 0.01,
    overlap: float = 1.0,
    n_clutter: int = 0,
    seed: int = 0,
) -> SyntheticScene:
    """Generate a transformed, noisy copy of a synthetic cloud.

    ``overlap`` < 1: the target contains only a random ``overlap`` fraction
    of the transformed source points (the rest simply do not exist there).
    ``n_clutter`` > 0: extra unrelated points are appended to the target in
    a region offset from the true cloud, simulating partial scans.
    """
    if not 0.0 < overlap <= 1.0:
        raise ValueError("overlap must be in (0, 1]")
    rng = np.random.default_rng(seed)

    source = _points(kind, n_points, rng)

    axis = rng.normal(size=3)
    R_true = rotation_about_axis(axis, angle_deg)
    direction = rng.normal(size=3)
    direction /= np.linalg.norm(direction)
    t_true = translation * direction

    transformed = (R_true @ source.T).T + t_true

    n_keep = max(1, int(round(overlap * n_points)))
    keep = rng.choice(n_points, size=n_keep, replace=False)
    keep.sort()
    target = transformed[keep].copy()
    if noise_std > 0:
        target += rng.normal(scale=noise_std, size=target.shape)

    if n_clutter > 0:
        clutter = rng.uniform(3.0, 6.0, size=(n_clutter, 3))
        clutter[:, 2] = rng.uniform(-1.0, 1.0, size=n_clutter)
        target = np.vstack([target, clutter])

    return SyntheticScene(
        source=source,
        target=target,
        R_true=R_true,
        t_true=t_true,
        overlap=overlap,
        noise_std=noise_std,
        kind=kind,
    )
