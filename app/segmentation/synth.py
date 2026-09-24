"""Deterministic synthetic point clouds with constructed ground truth.

Every point is generated from a known parametric surface, so its label is
known by construction (not hand-picked):

* ground grid points lying on a plane (flat or inclined) -> ``ground``
* vertical walls / poles / elevated clutter                 -> ``non_ground``
* scenes deliberately below the reliability gates         -> ``unknown``

All randomness goes through a seeded ``numpy.random.Generator``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

GROUND = "ground"
NON_GROUND = "non_ground"
UNKNOWN = "unknown"


@dataclass
class Scene:
    name: str
    points: np.ndarray            # (N, 3), global coordinates, metres
    labels: list[str]             # constructed truth, aligned with points
    description: str
    params: dict


class _Builder:
    def __init__(self) -> None:
        self.coords: list[np.ndarray] = []
        self.labels: list[str] = []

    def add(self, pts: np.ndarray, label: str) -> None:
        pts = np.asarray(pts, dtype=np.float64).reshape(-1, 3)
        self.coords.append(pts)
        self.labels.extend([label] * len(pts))

    def build(self, name: str, description: str, params: dict,
              rng: np.random.Generator, duplicate_ratio: float = 0.0,
              duplicate_jitter: float = 0.0) -> Scene:
        coords = np.vstack(self.coords)
        labels = list(self.labels)
        if duplicate_ratio > 0.0:
            n_dup = int(round(duplicate_ratio * len(coords)))
            pick = rng.integers(0, len(coords), size=n_dup)
            extra = coords[pick].copy()
            if duplicate_jitter > 0.0:
                extra += rng.normal(scale=duplicate_jitter, size=extra.shape)
            coords = np.vstack([coords, extra])
            labels.extend(labels[i] for i in pick)
        return Scene(name, coords, labels, description, params)


def _grid_ground(
    rng: np.random.Generator,
    extent: tuple[float, float],
    spacing: float = 0.4,
    angle_deg: float = 0.0,
    strike: float = 0.0,
    sigma: float = 0.01,
    z_offset: float = 0.0,
) -> np.ndarray:
    """Planar ground patch tilted ``angle_deg`` (about axis ``strike``)."""
    xs = np.arange(0.0, extent[0] + 1e-9, spacing)
    ys = np.arange(0.0, extent[1] + 1e-9, spacing)
    xx, yy = np.meshgrid(xs, ys)
    # Height field: slope along a direction rotated by `strike`.
    theta = math.radians(angle_deg)
    phi = math.radians(strike)
    zz = math.tan(theta) * (xx * math.cos(phi) + yy * math.sin(phi)) + z_offset
    pts = np.column_stack([xx.ravel(), yy.ravel(), zz.ravel()])
    pts[:, 2] += rng.normal(scale=sigma, size=len(pts))
    return pts


def _vertical_wall(rng: np.random.Generator, x: float, y_extent: tuple[float, float],
                   z_top: float = 2.0, spacing: float = 0.4) -> np.ndarray:
    """Wall plane x = const, standing on (but not touching) the ground."""
    ys = np.arange(y_extent[0], y_extent[1] + 1e-9, spacing)
    zs = np.arange(0.25, z_top + 1e-9, spacing)
    yy, zz = np.meshgrid(ys, zs)
    pts = np.column_stack([np.full(yy.size, x), yy.ravel(), zz.ravel()])
    pts += rng.normal(scale=0.005, size=pts.shape)
    return pts


def _poles(rng: np.random.Generator, positions: list[tuple[float, float]],
           height: float = 1.6, spacing: float = 0.2) -> np.ndarray:
    out = []
    zs = np.arange(0.15, height + 1e-9, spacing)
    for x, y in positions:
        for z in zs:
            out.append([x + rng.normal(scale=0.003),
                        y + rng.normal(scale=0.003), z])
    return np.asarray(out, dtype=np.float64)


def _volume_noise(rng: np.random.Generator, n: int,
                  extent_xy: tuple[float, float],
                  z_range: tuple[float, float],
                  avoid_band: tuple[float, float, float] | None = None) -> np.ndarray:
    """Uniform clutter above the ground.

    ``avoid_band`` optionally excludes points whose z would land inside
    ``(z0, z1)`` at their (x, y) on a plane through origin, keeping the
    ground band clean when the test needs it.
    """
    pts = rng.uniform(
        low=[0.0, 0.0, z_range[0]],
        high=[extent_xy[0], extent_xy[1], z_range[1]],
        size=(n, 3),
    )
    if avoid_band is not None:
        z0, z1, slope_z_per_xy = avoid_band
        plane_z = slope_z_per_xy * pts[:, 0]
        keep = (pts[:, 2] < plane_z + z0) | (pts[:, 2] > plane_z + z1)
        pts = pts[keep]
    return pts


# ---------------------------------------------------------------- scenes ---

def scene_flat_with_wall(seed: int = 101) -> Scene:
    rng = np.random.default_rng(seed)
    b = _Builder()
    b.add(_grid_ground(rng, (8.0, 8.0), spacing=0.4, sigma=0.01), GROUND)
    b.add(_vertical_wall(rng, x=5.0, y_extent=(0.5, 7.5), z_top=2.0), NON_GROUND)
    b.add(_poles(rng, [(1.5, 6.2), (6.6, 1.4)]), NON_GROUND)
    return b.build(
        "flat_with_wall",
        "Flat noisy ground, a vertical wall and two poles on an 8x8 m area; "
        "forces the wall through the 20-degree tilt gate so it can never be "
        "labelled ground just for being the largest plane.",
        {"seed": seed, "extent_m": [8.0, 8.0]},
        rng,
    )


def scene_slope(seed: int = 202, angle_deg: float = 12.0) -> Scene:
    rng = np.random.default_rng(seed)
    b = _Builder()
    b.add(_grid_ground(rng, (8.0, 6.0), spacing=0.4,
                       angle_deg=angle_deg, sigma=0.01), GROUND)
    b.add(_poles(rng, [(1.0, 1.0), (6.8, 4.8), (3.4, 3.0)], height=1.4), NON_GROUND)
    return b.build(
        "slope",
        f"Uniform {angle_deg}-degree inclined plane (inside the 20-degree "
        "limit) with three vertical poles; the inclined surface itself is "
        "constructed ground truth.",
        {"seed": seed, "slope_deg": angle_deg, "extent_m": [8.0, 6.0]},
        rng,
    )


def scene_steep_slope(seed: int = 303, angle_deg: float = 32.0) -> Scene:
    rng = np.random.default_rng(seed)
    b = _Builder()
    # A steep embankment: geometrically a fine plane, semantically NOT ground
    # under the configured 20-degree limit.  Truth is unknown/non-ground; the
    # algorithm must abstain rather than call its biggest plane "ground".
    b.add(_grid_ground(rng, (8.0, 4.0), spacing=0.35,
                       angle_deg=angle_deg, sigma=0.01), UNKNOWN)
    return b.build(
        "steep_slope",
        f"Steep {angle_deg}-degree embankment: a strong planar fit that "
        "violates the max-tilt gate.  Expected verdict is undecidable, never "
        "ground.",
        {"seed": seed, "slope_deg": angle_deg, "extent_m": [8.0, 4.0]},
        rng,
    )


def scene_sparse(seed: int = 404) -> Scene:
    rng = np.random.default_rng(seed)
    b = _Builder()
    pts = _grid_ground(rng, (1.0, 1.0), spacing=0.3, sigma=0.005)
    pts = pts[:8]  # fewer than the 12-point minimum
    b.add(pts, UNKNOWN)
    return b.build(
        "sparse",
        "Only 8 points: below the minimum sample size.  No plane claim is "
        "possible; every point must remain unknown.",
        {"seed": seed, "point_count": 8},
        rng,
    )


def scene_noise_dominated(seed: int = 505) -> Scene:
    rng = np.random.default_rng(seed)
    b = _Builder()
    ground = _grid_ground(rng, (2.0, 2.0), spacing=0.5, sigma=0.01)
    # 300 clutter points vs 25 ground points: even after a lucky triple the
    # inlier ratio stays far below 0.5, so no plane may be declared reliable.
    noise = _volume_noise(rng, n=300, extent_xy=(2.0, 2.0),
                          z_range=(0.0, 3.0),
                          avoid_band=(0.3, 2.6, 0.0))
    b.add(ground, UNKNOWN)
    b.add(noise, UNKNOWN)
    return b.build(
        "noise_dominated",
        f"Only {len(ground)} ground-ish points drowned in {len(noise)} "
        "uniform volume-noise points: inlier ratio is far below 0.5, so no "
        "fit can be judged reliable.  The correct output is undecidable for "
        "every point - abstaining beats locking onto a spurious largest plane.",
        {"seed": seed, "ground_points": len(ground), "noise_points": len(noise)},
        rng,
    )


def scene_moderate_noise(seed: int = 506) -> Scene:
    rng = np.random.default_rng(seed)
    b = _Builder()
    ground = _grid_ground(rng, (3.0, 3.0), spacing=0.3, sigma=0.012)
    noise = _volume_noise(rng, n=25, extent_xy=(3.0, 3.0),
                          z_range=(0.0, 2.5),
                          avoid_band=(0.25, 2.2, 0.0))
    b.add(ground, GROUND)
    b.add(noise, NON_GROUND)
    return b.build(
        "moderate_noise",
        f"Single-chunk cloud with {len(ground)} ground points and "
        f"{len(noise)} elevated noise points (~{len(noise)/(len(noise)+len(ground)):.0%}). "
        "The plane stays reliable; noise above the distance band is non-ground.",
        {"seed": seed, "ground_points": len(ground), "noise_points": len(noise)},
        rng,
    )


def scene_duplicates(seed: int = 607) -> Scene:
    rng = np.random.default_rng(seed)
    b = _Builder()
    b.add(_grid_ground(rng, (3.0, 3.0), spacing=0.5, sigma=0.005), GROUND)
    b.add(_vertical_wall(rng, x=2.9, y_extent=(0.1, 2.9), z_top=1.5,
                         spacing=0.5), NON_GROUND)
    # 40% of rows are exact repeated coordinates (zero jitter).
    return b.build(
        "duplicates",
        "Single-chunk flat ground plus a vertical wall, with ~40% exact "
        "duplicate rows.  Duplicates must not corrupt sampling (three distinct "
        "coordinates per triple) or inflate precision/recall inconsistently.",
        {"seed": seed, "duplicate_ratio": 0.4, "duplicate_jitter": 0.0},
        rng,
        duplicate_ratio=0.4,
        duplicate_jitter=0.0,
    )


def scene_mixed_world(seed: int = 707) -> Scene:
    rng = np.random.default_rng(seed)
    b = _Builder()
    b.add(_grid_ground(rng, (8.0, 6.0), spacing=0.4,
                       angle_deg=10.0, strike=15.0, sigma=0.012), GROUND)
    b.add(_vertical_wall(rng, x=5.5, y_extent=(0.5, 5.5), z_top=2.2), NON_GROUND)
    b.add(_poles(rng, [(1.2, 1.2), (7.0, 4.7)]), NON_GROUND)
    # A handful of clearly elevated noise points (kept away from the band).
    slope_per_x = math.tan(math.radians(10.0)) * math.cos(math.radians(15.0))
    noise = _volume_noise(rng, n=30, extent_xy=(8.0, 6.0),
                          z_range=(0.0, 3.5),
                          avoid_band=(0.35, 2.5, slope_per_x))
    b.add(noise, NON_GROUND)
    return b.build(
        "mixed_world",
        "10-degree sloping ground, one vertical wall, two poles, elevated "
        "noise and ~5% near-duplicate rows across several overlapping chunks "
        "- the end-to-end example scenario.",
        {"seed": seed, "slope_deg": 10.0, "strike_deg": 15.0,
         "extent_m": [8.0, 6.0], "noise_points": len(noise)},
        rng,
        duplicate_ratio=0.05,
        duplicate_jitter=1e-6,
    )


SCENES = {
    "flat_with_wall": scene_flat_with_wall,
    "slope": scene_slope,
    "steep_slope": scene_steep_slope,
    "sparse": scene_sparse,
    "noise_dominated": scene_noise_dominated,
    "moderate_noise": scene_moderate_noise,
    "duplicates": scene_duplicates,
    "mixed_world": scene_mixed_world,
}


def build_scene(name: str, seed: int | None = None) -> Scene:
    if name not in SCENES:
        raise KeyError(f"unknown scene {name!r}; choose from {sorted(SCENES)}")
    return SCENES[name]() if seed is None else SCENES[name](seed)
