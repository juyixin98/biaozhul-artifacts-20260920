"""Seeded, deterministic RANSAC plane fitting with explicit reliability gates.

Design notes
------------
* "Seeded" is used in the ground-segmentation sense: candidate triples are
  preferentially sampled from a low-elevation seed band (the points most
  likely to lie on the ground).  An explicit RNG ``seed`` additionally makes
  every run bit-for-bit reproducible.
* The *largest* plane found is **never** automatically accepted as ground.
  A plane is reported ``reliable`` only when it passes every gate:
  minimum inlier count, minimum inlier ratio, maximum tilt and maximum RMS
  roughness.  Otherwise the result is ``undecidable`` with a machine-readable
  ``reason``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .geometry import Plane, fit_plane, plane_from_three

# Machine-readable status reasons.
REASON_RELIABLE = "reliable"
REASON_TOO_FEW_POINTS = "too_few_points"
REASON_NO_VALID_SAMPLE = "no_valid_sample"
REASON_TILT_EXCEEDED = "tilt_exceeded"
REASON_LOW_INLIER_RATIO = "low_inlier_ratio"
REASON_TOO_FEW_INLIERS = "too_few_inliers"
REASON_PLANE_TOO_ROUGH = "plane_too_rough"


@dataclass(frozen=True)
class RansacConfig:
    """Configuration for :func:`ransac_plane` (all distances in metres)."""

    distance_threshold: float = 0.10
    max_iterations: int = 1000
    min_iterations: int = 20
    confidence: float = 0.99          # adaptive-iteration target probability
    min_points: int = 12               # below this the tile is undecidable
    min_inlier_count: int = 8
    min_inlier_ratio: float = 0.50
    max_tilt_deg: float = 20.0
    max_rms: float = 0.05              # inlier-plane RMS after refit
    seed_band_quantile: float = 0.25   # lowest-z fraction used as seeds
    seed_sample_probability: float = 0.7
    rng_seed: int = 20260923
    refit_rounds: int = 2


@dataclass
class RansacResult:
    """Outcome of a RANSAC fit on one point set (a tile or a whole cloud)."""

    status: str                        # "reliable" | "undecidable"
    reason: str
    plane: Plane | None
    inlier_mask: np.ndarray            # bool mask over the *input* points
    iterations_used: int
    candidate_samples: int
    stats: dict = field(default_factory=dict)

    @property
    def reliable(self) -> bool:
        return self.status == "reliable"


def _unique_index_groups(points: np.ndarray) -> list[np.ndarray]:
    """Group row indices by identical (x, y, z) coordinate (duplicate-aware)."""
    # View as void rows to get exact byte-identical grouping.
    order = np.lexsort((points[:, 2], points[:, 1], points[:, 0]))
    sorted_pts = points[order]
    diff = np.any(np.diff(sorted_pts, axis=0) != 0.0, axis=1)
    boundaries = np.concatenate(([0], np.flatnonzero(diff) + 1, [len(points)]))
    groups = [order[boundaries[i]:boundaries[i + 1]] for i in range(len(boundaries) - 1)]
    return groups


def _adaptive_iterations(inlier_ratio: float, cfg: RansacConfig) -> int:
    """RANSAC textbook update: samples needed to see an all-inlier triple."""
    w = min(max(inlier_ratio, 1e-6), 1.0 - 1e-12)
    denom = math.log(1.0 - w**3)
    if denom >= 0.0:
        return cfg.max_iterations
    n = math.log(1.0 - cfg.confidence) / denom
    return int(min(cfg.max_iterations, max(cfg.min_iterations, math.ceil(n))))


def ransac_plane(points: np.ndarray, cfg: RansacConfig | None = None) -> RansacResult:
    """Fit a single plane with seeded RANSAC.

    Parameters
    ----------
    points:
        ``(N, 3)`` array in one consistent global coordinate frame.  Duplicate
        coordinates are allowed; they count toward inlier statistics but are
        never drawn as three *distinct* sample coordinates.
    cfg:
        :class:`RansacConfig`; sensible defaults are used when omitted.
    """
    cfg = cfg or RansacConfig()
    pts = np.asarray(points, dtype=np.float64)
    n = len(pts)
    mask = np.zeros(n, dtype=bool)

    if n < cfg.min_points:
        return RansacResult(
            status="undecidable",
            reason=REASON_TOO_FEW_POINTS,
            plane=None,
            inlier_mask=mask,
            iterations_used=0,
            candidate_samples=0,
            stats={"point_count": int(n)},
        )

    rng = np.random.default_rng(cfg.rng_seed)

    # One representative index per distinct coordinate: sampling uses distinct
    # points, while inlier masks are expanded to all duplicate copies later.
    groups = _unique_index_groups(pts)
    uniq_idx = np.array([int(g[0]) for g in groups], dtype=np.int64)
    n_uniq = len(uniq_idx)
    if n_uniq < 3:
        return RansacResult(
            status="undecidable",
            reason=REASON_NO_VALID_SAMPLE,
            plane=None,
            inlier_mask=mask,
            iterations_used=0,
            candidate_samples=0,
            stats={"point_count": int(n), "unique_count": int(n_uniq)},
        )

    uniq_pts = pts[uniq_idx]
    # Seed set: lowest-z band of the distinct points.
    band_size = max(3, int(math.ceil(cfg.seed_band_quantile * n_uniq)))
    seed_local = np.argsort(uniq_pts[:, 2])[:band_size]

    thr = cfg.distance_threshold
    best_count = -1
    best_error = np.inf
    best_plane: Plane | None = None

    def score(plane: Plane) -> tuple[int, float, np.ndarray]:
        dist = uniq_pts @ plane.normal + plane.offset
        inl = np.abs(dist) <= thr
        cnt = int(inl.sum())
        # Tie-break on inlier tightness (sum of squared within-threshold error).
        err = float(np.sum(dist[inl] ** 2))
        return cnt, err, inl

    iterations = 0
    attempts = 0
    target_iterations = cfg.min_iterations
    rejected_tilt = False
    while iterations < target_iterations and iterations < cfg.max_iterations:
        if rng.random() < cfg.seed_sample_probability and len(seed_local) >= 3:
            pick = rng.choice(seed_local, size=3, replace=False)
        else:
            pick = rng.choice(n_uniq, size=3, replace=False)
        attempts += 1
        sample = uniq_pts[pick]
        try:
            plane = plane_from_three(sample, orientation="up")
        except ValueError:
            continue
        cnt, err, inl = score(plane)
        if cnt > best_count or (cnt == best_count and err < best_error):
            best_count, best_error, best_plane = cnt, err, plane
            rejected_tilt = rejected_tilt or plane.tilt_deg > cfg.max_tilt_deg
        iterations += 1
        ratio = best_count / n_uniq
        target_iterations = _adaptive_iterations(ratio, cfg)

    if best_plane is None or best_count < 3:
        return RansacResult(
            status="undecidable",
            reason=REASON_NO_VALID_SAMPLE,
            plane=None,
            inlier_mask=mask,
            iterations_used=iterations,
            candidate_samples=attempts,
            stats={"point_count": int(n), "unique_count": int(n_uniq)},
        )

    # Least-squares refit on the inliers, repeated until the set stabilises.
    plane = best_plane
    inl = np.abs(uniq_pts @ plane.normal + plane.offset) <= thr
    for _ in range(max(0, cfg.refit_rounds)):
        try:
            plane = fit_plane(uniq_pts[inl], orientation="up")
        except ValueError:
            break
        new_inl = np.abs(uniq_pts @ plane.normal + plane.offset) <= thr
        if np.array_equal(new_inl, inl):
            inl = new_inl
            break
        inl = new_inl

    inlier_uniq = uniq_idx[inl]
    inlier_count = int(len(inlier_uniq))
    # Expand to include every duplicate copy of an inlier coordinate.
    full_mask = np.zeros(n, dtype=bool)
    full_mask[inlier_uniq] = True
    for g in groups:
        if full_mask[g[0]]:
            full_mask[g] = True
    total_inliers = int(full_mask.sum())
    ratio_unique = inlier_count / n_uniq
    ratio_all = total_inliers / n
    dist = pts[full_mask] @ plane.normal + plane.offset
    rms = float(np.sqrt(np.mean(dist**2))) if total_inliers else float("inf")

    final_plane = Plane(
        normal=plane.normal,
        offset=plane.offset,
        inlier_count=total_inliers,
        inlier_ratio=ratio_all,
        rms=rms,
    )

    stats = {
        "point_count": int(n),
        "unique_count": int(n_uniq),
        "inlier_count": total_inliers,
        "inlier_count_unique": inlier_count,
        "inlier_ratio": ratio_all,
        "inlier_ratio_unique": ratio_unique,
        "tilt_deg": final_plane.tilt_deg,
        "rms": rms,
        "iterations_used": iterations,
        "candidate_samples": attempts,
        "saw_steep_candidate": bool(rejected_tilt),
    }

    # Explicit reliability gates — order matters for the reported reason.
    if final_plane.tilt_deg > cfg.max_tilt_deg:
        reason = REASON_TILT_EXCEEDED
    elif inlier_count < cfg.min_inlier_count:
        reason = REASON_TOO_FEW_INLIERS
    elif ratio_unique < cfg.min_inlier_ratio:
        reason = REASON_LOW_INLIER_RATIO
    elif rms > cfg.max_rms:
        reason = REASON_PLANE_TOO_ROUGH
    else:
        reason = REASON_RELIABLE

    status = "reliable" if reason == REASON_RELIABLE else "undecidable"
    return RansacResult(
        status=status,
        reason=reason,
        plane=final_plane if status == "reliable" else None,
        inlier_mask=full_mask if status == "reliable" else np.zeros(n, dtype=bool),
        iterations_used=iterations,
        candidate_samples=attempts,
        stats=stats,
    )
