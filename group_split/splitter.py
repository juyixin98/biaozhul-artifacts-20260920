"""Group-constrained stratified splitting.

Splits samples into named splits (e.g. train/val/test) under a hard
constraint and a soft objective:

* Hard constraint — group isolation: every sample of a group lands in
  exactly one split. Never violated.
* Hard constraint — total conservation: every sample is assigned to
  exactly one split. Never violated.
* Soft objective — stratification: each split's class distribution should
  match the requested ratios as closely as group granularity allows.

Algorithm
---------
1. Aggregate samples into per-group class-count vectors.
2. Compute per-split target counts: ``ratio[s] * total`` for every class
   and for the split size itself.
3. Order groups deterministically by ``(-group_size, hash(seed, group_id))``.
   Sorting by a seeded hash of the *group id* (never the input position)
   makes the result invariant to input row ordering, and independent of
   ``PYTHONHASHSEED`` because the hash is BLAKE2b, not ``hash()``.
4. Greedily assign each group to the split that minimises the total L1
   deviation from target (class counts + split size). Ties are broken by
   ``hash(seed, group_id, split_name)``.
5. Measure the residual deviation and, when it exceeds the tolerance,
   attach machine-readable reasons (oversized groups, rare classes
   concentrated in too few groups, granularity).

When group isolation and perfect stratification conflict, isolation always
wins and the report explains why.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Hashable, Mapping, Sequence

import numpy as np

DEFAULT_TOLERANCE = 0.05
_RATIO_SUM_TOL = 1e-9
_COST_TIE_TOL = 1e-12

# Reason codes reported in SplitResult.report["reasons"].
REASON_LARGE_GROUP = "LARGE_GROUP"
REASON_RARE_CLASS = "RARE_CLASS"
REASON_RESIDUAL_IMBALANCE = "RESIDUAL_IMBALANCE"


def _stable_hash(*parts: Hashable) -> int:
    """Deterministic 64-bit hash, independent of PYTHONHASHSEED."""
    key = "\x1f".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(), "big")


def _to_py(value: Any) -> Any:
    """Convert numpy scalars to plain Python objects for JSON safety."""
    return value.item() if isinstance(value, np.generic) else value


@dataclass
class SplitResult:
    """Outcome of :func:`split_groups`.

    Attributes:
        assignment: mapping of group id -> split name.
        split_indices: mapping of split name -> sorted sample indices.
        report: JSON-serialisable deviation report (see module docstring).
    """

    assignment: dict[Hashable, str]
    split_indices: dict[str, np.ndarray]
    report: dict[str, Any] = field(default_factory=dict)

    @property
    def within_tolerance(self) -> bool:
        return bool(self.report["within_tolerance"])


def _validate(
    groups: np.ndarray,
    labels: np.ndarray,
    ratios: Mapping[str, float],
    tolerance: float,
) -> None:
    if groups.ndim != 1 or labels.ndim != 1:
        raise ValueError("groups and labels must be 1-D arrays")
    if groups.shape[0] != labels.shape[0]:
        raise ValueError(
            f"groups and labels length mismatch: "
            f"{groups.shape[0]} != {labels.shape[0]}"
        )
    if groups.shape[0] == 0:
        raise ValueError("cannot split an empty dataset")
    if not ratios:
        raise ValueError("ratios must name at least one split")
    for name, ratio in ratios.items():
        if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
            raise ValueError(f"ratio for split {name!r} must be a number")
        if ratio <= 0:
            raise ValueError(f"ratio for split {name!r} must be positive")
    total = sum(float(r) for r in ratios.values())
    if abs(total - 1.0) > _RATIO_SUM_TOL:
        raise ValueError(f"ratios must sum to 1.0, got {total!r}")
    if tolerance <= 0:
        raise ValueError("tolerance must be positive")


def split_groups(
    groups: Sequence[Hashable] | np.ndarray,
    labels: Sequence[Hashable] | np.ndarray,
    ratios: Mapping[str, float],
    seed: int = 0,
    tolerance: float = DEFAULT_TOLERANCE,
) -> SplitResult:
    """Split samples into named splits, keeping every group in one split.

    Args:
        groups: group id per sample (any hashable dtype, length N).
        labels: class label per sample (length N).
        ratios: mapping of split name -> target fraction of samples.
            Values must be positive and sum to 1.0.
        seed: integer seed; identical inputs and seed give identical output.
        tolerance: maximum acceptable relative deviation (per class per
            split, and per split size) before the report flags reasons.

    Returns:
        SplitResult with the group assignment, per-split sample indices and
        a deviation report.

    Raises:
        ValueError: on malformed input (length mismatch, empty data,
            non-positive or mis-summed ratios, non-positive tolerance).
    """
    groups_arr = np.asarray(groups)
    labels_arr = np.asarray(labels)
    _validate(groups_arr, labels_arr, ratios, tolerance)

    split_names = list(ratios.keys())
    ratio_vec = np.array([float(ratios[n]) for n in split_names])
    n_splits = len(split_names)

    try:
        unique_labels, label_idx = np.unique(labels_arr, return_inverse=True)
        unique_groups, group_idx = np.unique(groups_arr, return_inverse=True)
    except TypeError as exc:
        raise ValueError(
            "groups and labels must each contain mutually comparable values"
        ) from exc

    n_classes = len(unique_labels)
    n_groups = len(unique_groups)

    # Per-group class-count matrix, shape (n_groups, n_classes).
    group_class_counts = np.zeros((n_groups, n_classes), dtype=np.int64)
    np.add.at(group_class_counts, (group_idx, label_idx), 1)
    group_sizes = group_class_counts.sum(axis=1)

    class_totals = group_class_counts.sum(axis=0).astype(np.float64)
    n_samples = int(group_sizes.sum())

    # Targets, shape (n_splits, n_classes + 1); last column is split size.
    target = np.empty((n_splits, n_classes + 1))
    target[:, :n_classes] = ratio_vec[:, None] * class_totals[None, :]
    target[:, n_classes] = ratio_vec * n_samples

    # Deterministic order: largest groups first, seeded hash as tie-break.
    order = sorted(
        range(n_groups),
        key=lambda g: (-int(group_sizes[g]), _stable_hash(seed, unique_groups[g])),
    )

    current = np.zeros((n_splits, n_classes + 1))
    assignment_idx = np.empty(n_groups, dtype=np.int64)

    for g in order:
        vec = np.empty(n_classes + 1)
        vec[:n_classes] = group_class_counts[g]
        vec[n_classes] = group_sizes[g]
        best_split = -1
        best_cost = np.inf
        best_hash = -1
        for s in range(n_splits):
            delta = float(
                (
                    np.abs(current[s] + vec - target[s])
                    - np.abs(current[s] - target[s])
                ).sum()
            )
            tie_hash = _stable_hash(seed, unique_groups[g], split_names[s])
            if (
                delta < best_cost - _COST_TIE_TOL
                or (abs(delta - best_cost) <= _COST_TIE_TOL and tie_hash < best_hash)
                or best_split < 0
            ):
                best_split, best_cost, best_hash = s, delta, tie_hash
        current[best_split] += vec
        assignment_idx[g] = best_split

    assignment = {
        _to_py(unique_groups[g]): split_names[assignment_idx[g]] for g in range(n_groups)
    }
    sample_split = assignment_idx[group_idx]
    split_indices = {
        name: np.flatnonzero(sample_split == s) for s, name in enumerate(split_names)
    }
    report = _build_report(
        split_names=split_names,
        unique_labels=unique_labels,
        unique_groups=unique_groups,
        group_sizes=group_sizes,
        group_class_counts=group_class_counts,
        class_totals=class_totals,
        target=target,
        current=current,
        assignment_idx=assignment_idx,
        seed=seed,
        n_samples=n_samples,
        tolerance=tolerance,
    )
    return SplitResult(assignment=assignment, split_indices=split_indices, report=report)


def _build_report(
    *,
    split_names: list[str],
    unique_labels: np.ndarray,
    unique_groups: np.ndarray,
    group_sizes: np.ndarray,
    group_class_counts: np.ndarray,
    class_totals: np.ndarray,
    target: np.ndarray,
    current: np.ndarray,
    assignment_idx: np.ndarray,
    seed: int,
    n_samples: int,
    tolerance: float,
) -> dict[str, Any]:
    n_classes = len(unique_labels)
    label_names = [str(_to_py(c)) for c in unique_labels]

    splits_report: dict[str, Any] = {}
    max_rel_dev = 0.0
    for s, name in enumerate(split_names):
        size = int(current[s, n_classes])
        target_size = float(target[s, n_classes])
        size_dev = (size - target_size) / max(target_size, 1.0)
        class_dev: dict[str, float] = {}
        for c, label in enumerate(label_names):
            actual = float(current[s, c])
            tgt = float(target[s, c])
            rel = (actual - tgt) / max(tgt, 1.0)
            class_dev[label] = rel
            max_rel_dev = max(max_rel_dev, abs(rel))
        max_rel_dev = max(max_rel_dev, abs(size_dev))
        splits_report[name] = {
            "size": size,
            "target_size": target_size,
            "size_deviation": size_dev,
            "class_counts": {
                label: int(current[s, c]) for c, label in enumerate(label_names)
            },
            "target_class_counts": {
                label: float(target[s, c]) for c, label in enumerate(label_names)
            },
            "class_deviation": class_dev,
        }

    reasons = _diagnose_reasons(
        split_names=split_names,
        label_names=label_names,
        unique_groups=unique_groups,
        group_sizes=group_sizes,
        group_class_counts=group_class_counts,
        class_totals=class_totals,
        target=target,
        current=current,
        assignment_idx=assignment_idx,
        n_classes=n_classes,
    )
    within = max_rel_dev <= tolerance
    if not within and not reasons:
        reasons.append(
            {
                "code": REASON_RESIDUAL_IMBALANCE,
                "detail": (
                    "no oversized group or rare class explains the deviation; "
                    "group granularity is too coarse for the requested ratios"
                ),
            }
        )

    return {
        "seed": seed,
        "n_samples": n_samples,
        "n_groups": int(len(unique_groups)),
        "n_classes": n_classes,
        "tolerance": tolerance,
        "within_tolerance": within,
        "max_deviation": max_rel_dev,
        "splits": splits_report,
        "reasons": reasons,
    }


def _diagnose_reasons(
    *,
    split_names: list[str],
    label_names: list[str],
    unique_groups: np.ndarray,
    group_sizes: np.ndarray,
    group_class_counts: np.ndarray,
    class_totals: np.ndarray,
    target: np.ndarray,
    current: np.ndarray,
    assignment_idx: np.ndarray,
    n_classes: int,
) -> list[dict[str, str]]:
    reasons: list[dict[str, str]] = []
    n_splits = len(split_names)

    # Oversized groups: a group larger than the smallest split's target
    # size cannot fit everywhere, which constrains placement; if it also
    # exceeds its assigned split's target, overshoot is unavoidable.
    min_target_size = float(target[:, n_classes].min())
    for g in range(len(unique_groups)):
        if int(group_sizes[g]) <= min_target_size:
            continue
        s = int(assignment_idx[g])
        overshoot = int(group_sizes[g]) > target[s, n_classes]
        reasons.append(
            {
                "code": REASON_LARGE_GROUP,
                "detail": (
                    f"group {_to_py(unique_groups[g])!r} has "
                    f"{int(group_sizes[g])} samples, exceeding the smallest "
                    f"split's target size {min_target_size:.1f}"
                    + (
                        f" and the target {float(target[s, n_classes]):.1f} "
                        f"of its assigned split {split_names[s]!r}"
                        if overshoot
                        else ""
                    )
                    + "; group isolation forbids dividing it"
                ),
            }
        )

    # Rare classes: a class living in fewer groups than there are splits
    # must be absent from at least one split.
    for c, label in enumerate(label_names):
        if class_totals[c] == 0:
            continue
        groups_with_class = np.flatnonzero(group_class_counts[:, c] > 0)
        if len(groups_with_class) < n_splits:
            empty_splits = [
                split_names[s] for s in range(n_splits) if current[s, c] == 0
            ]
            reasons.append(
                {
                    "code": REASON_RARE_CLASS,
                    "detail": (
                        f"class {label!r} appears in only "
                        f"{len(groups_with_class)} group(s) "
                        f"({int(class_totals[c])} samples), fewer than the "
                        f"{n_splits} splits; split(s) {empty_splits} contain "
                        f"no sample of it"
                    ),
                }
            )
    return reasons
