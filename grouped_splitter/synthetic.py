"""Reproducible synthetic dataset with explicit group structure.

The generator deliberately creates the two hard cases the splitter must handle:

* **large groups** -- a few atomic groups holding a sizable fraction of all
  samples (they cannot be cut across splits);
* **rare classes** -- classes represented by a single small group, which makes
  it combinatorially impossible for the class to appear in every split.

Features carry a real (linearly separable) class signal plus a per-group random
shift, so the accompanying NumPy logistic-regression model has something to
learn and group identity matters.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True)
class Dataset:
    X: np.ndarray                 # (n_samples, n_features) float
    y: np.ndarray                 # (n_samples,) int labels
    group_ids: np.ndarray         # (n_samples,) str group ids
    class_names: tuple[str, ...]

    @property
    def n_samples(self) -> int:
        return int(self.X.shape[0])

    @property
    def n_features(self) -> int:
        return int(self.X.shape[1])

    @property
    def n_groups(self) -> int:
        return int(len(np.unique(self.group_ids)))

    def group_size_table(self) -> dict[str, int]:
        ids, counts = np.unique(self.group_ids, return_counts=True)
        return {str(g): int(c) for g, c in zip(ids, counts)}


def generate_synthetic_dataset(
    *,
    n_samples: int = 2000,
    n_features: int = 8,
    n_classes: int = 3,
    n_groups: int = 40,
    rare_class_count: int = 1,
    rare_group_size: int = 3,
    large_group_count: int = 1,
    large_group_fraction: float = 0.25,
    centroid_scale: float = 2.0,
    group_shift_scale: float = 0.8,
    seed: int = 42,
) -> Dataset:
    """Generate a reproducible grouped classification dataset.

    Labels ``0 .. rare_class_count-1`` are the rare classes: each owns exactly
    one group of ``rare_group_size`` samples. ``large_group_count`` further
    groups each receive roughly ``large_group_fraction / large_group_count``
    of the data. Remaining samples are spread over ordinary groups whose
    classes are drawn uniformly from the non-rare classes.
    """
    _validate_params(
        n_samples=n_samples,
        n_features=n_features,
        n_classes=n_classes,
        n_groups=n_groups,
        rare_class_count=rare_class_count,
        rare_group_size=rare_group_size,
        large_group_count=large_group_count,
        large_group_fraction=large_group_fraction,
    )
    rng = np.random.default_rng(seed)

    group_specs = _build_group_specs(
        n_samples=n_samples,
        n_classes=n_classes,
        n_groups=n_groups,
        rare_class_count=rare_class_count,
        rare_group_size=rare_group_size,
        large_group_count=large_group_count,
        large_group_fraction=large_group_fraction,
        rng=rng,
    )

    # Class centroids (fixed geometry per seed) and per-group shifts.
    centroids = rng.normal(0.0, centroid_scale, size=(n_classes, n_features))
    total_groups = len(group_specs)
    group_shifts = rng.normal(
        0.0, group_shift_scale, size=(total_groups, n_features)
    )

    X = np.empty((n_samples, n_features), dtype=np.float64)
    y = np.empty(n_samples, dtype=np.int64)
    group_ids = np.empty(n_samples, dtype=f"U{max(8, len(str(total_groups)) + 3)}")

    cursor = 0
    for pos, (label, size) in enumerate(group_specs):
        gid = f"g_{pos:05d}"
        end = cursor + size
        X[cursor:end] = (
            centroids[label]
            + group_shifts[pos]
            + rng.normal(0.0, 1.0, size=(size, n_features))
        )
        y[cursor:end] = label
        group_ids[cursor:end] = gid
        cursor = end

    class_names = tuple(
        f"rare_class_{c}" if c < rare_class_count else f"class_{c}"
        for c in range(n_classes)
    )
    return Dataset(X=X, y=y, group_ids=group_ids, class_names=class_names)


# --------------------------------------------------------------------------
# Group-size construction
# --------------------------------------------------------------------------

def _build_group_specs(
    *,
    n_samples: int,
    n_classes: int,
    n_groups: int,
    rare_class_count: int,
    rare_group_size: int,
    large_group_count: int,
    large_group_fraction: float,
    rng: np.random.Generator,
) -> list[tuple[int, int]]:
    """Return an ordered list of ``(label, size)`` group specifications."""
    specs: list[tuple[int, int]] = []

    # Rare classes: exactly one small group each.
    for c in range(rare_class_count):
        specs.append((c, rare_group_size))

    # Large atomic groups.
    large_total_target = int(round(n_samples * large_group_fraction))
    per_large = max(1, large_total_target // max(1, large_group_count))
    for _ in range(large_group_count):
        specs.append(
            (
                int(rng.integers(rare_class_count, n_classes)),
                per_large,
            )
        )

    reserved = sum(size for _, size in specs)
    if reserved >= n_samples:
        raise ValueError(
            "rare + large groups already consume the whole dataset; "
            "reduce rare_group_size or large_group_fraction"
        )

    # Ordinary groups absorb the remainder with exact integer apportionment.
    remaining = n_samples - reserved
    n_ordinary = min(n_groups, remaining)
    weights = rng.exponential(1.0, size=n_ordinary)
    sizes = _largest_remainder_apportion(remaining, weights)
    for size in sizes:
        label = int(rng.integers(rare_class_count, n_classes))
        specs.append((label, int(size)))

    assert sum(size for _, size in specs) == n_samples
    return specs


def _largest_remainder_apportion(
    total: int, weights: np.ndarray
) -> np.ndarray:
    """Apportion exactly ``total`` units proportional to ``weights``."""
    weights = np.asarray(weights, dtype=np.float64)
    weights = weights / weights.sum()
    raw = total * weights
    floors = np.floor(raw).astype(np.int64)
    shortfall = int(total - int(floors.sum()))
    order = np.argsort(-(raw - floors), kind="stable")
    floors[order[:shortfall]] += 1
    return floors


def _validate_params(**params: Any) -> None:
    if params["n_samples"] <= 0:
        raise ValueError("n_samples must be positive")
    if params["n_features"] <= 0:
        raise ValueError("n_features must be positive")
    if params["n_classes"] < 2:
        raise ValueError("n_classes must be >= 2")
    if not 0 <= params["rare_class_count"] < params["n_classes"]:
        raise ValueError("rare_class_count must be in [0, n_classes)")
    if params["rare_group_size"] < 1:
        raise ValueError("rare_group_size must be >= 1")
    if params["large_group_count"] < 0:
        raise ValueError("large_group_count must be >= 0")
    if not 0.0 < params["large_group_fraction"] < 1.0:
        raise ValueError("large_group_fraction must be in (0, 1)")
    if params["n_groups"] < 1:
        raise ValueError("n_groups must be >= 1")
