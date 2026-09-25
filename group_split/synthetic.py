"""Reproducible synthetic datasets for exercising the splitter.

No external data or models are downloaded; everything is drawn from a
seeded ``numpy.random.Generator``.

Scenarios:
    make_balanced        -- many mid-size groups, 3 classes; stratification
                            should be achievable within tolerance.
    make_with_large_group -- one group holds a large fraction of all
                            samples, forcing a size/class deviation.
    make_with_rare_class  -- a rare class concentrated in a single small
                            group, so some split must miss it.
"""

from __future__ import annotations

import numpy as np

DEFAULT_CLASS_PRIOR = (0.7, 0.2, 0.1)


def _draw_groups(
    rng: np.random.Generator,
    n_groups: int,
    min_size: int,
    max_size: int,
) -> np.ndarray:
    """Return group sizes with a deterministic sum-friendly draw."""
    return rng.integers(min_size, max_size + 1, size=n_groups)


def _expand(
    rng: np.random.Generator,
    group_sizes: np.ndarray,
    class_prior: tuple[float, ...],
) -> tuple[np.ndarray, np.ndarray]:
    """Materialise per-sample group ids and labels from group sizes."""
    n_groups = len(group_sizes)
    groups = np.repeat(np.arange(n_groups), group_sizes)
    prior = np.asarray(class_prior, dtype=float)
    prior = prior / prior.sum()
    labels = rng.choice(len(prior), size=int(group_sizes.sum()), p=prior)
    return groups, labels


def make_balanced(
    seed: int = 0,
    n_groups: int = 300,
    min_size: int = 10,
    max_size: int = 60,
    class_prior: tuple[float, ...] = DEFAULT_CLASS_PRIOR,
) -> tuple[np.ndarray, np.ndarray]:
    """Many mid-size groups; class ratios are achievable."""
    rng = np.random.default_rng(seed)
    sizes = _draw_groups(rng, n_groups, min_size, max_size)
    return _expand(rng, sizes, class_prior)


def make_with_large_group(
    seed: int = 0,
    n_groups: int = 100,
    large_fraction: float = 0.45,
    class_prior: tuple[float, ...] = DEFAULT_CLASS_PRIOR,
) -> tuple[np.ndarray, np.ndarray]:
    """One group holds ``large_fraction`` of all samples.

    With a 70/15/15 split, no split can absorb a 45% group without a large
    deviation, so the report must flag a LARGE_GROUP reason.
    """
    if not 0.0 < large_fraction < 1.0:
        raise ValueError("large_fraction must be in (0, 1)")
    rng = np.random.default_rng(seed)
    small_sizes = _draw_groups(rng, n_groups - 1, 5, 20)
    large_size = int(round(large_fraction / (1 - large_fraction) * small_sizes.sum()))
    sizes = np.concatenate([small_sizes, [large_size]])
    return _expand(rng, sizes, class_prior)


def make_with_rare_class(
    seed: int = 0,
    n_groups: int = 200,
    rare_count: int = 4,
    class_prior: tuple[float, ...] = (0.75, 0.25),
) -> tuple[np.ndarray, np.ndarray]:
    """A rare class (label ``"rare"``) lives in a single small group.

    Fewer groups contain the class than there are splits, so at least one
    split gets none of it and the report must flag a RARE_CLASS reason.
    """
    rng = np.random.default_rng(seed)
    sizes = _draw_groups(rng, n_groups, 10, 40)
    groups, labels = _expand(rng, sizes, class_prior)
    # String labels so the rare class can share the array.
    labels = np.array([str(x) for x in labels], dtype=object)
    # Overwrite the tail samples of group 0 with the rare class.
    tail = np.flatnonzero(groups == 0)[-rare_count:]
    labels[tail] = "rare"
    return groups, labels
