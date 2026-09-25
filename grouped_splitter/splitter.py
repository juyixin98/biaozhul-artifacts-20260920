"""Core group-aware stratified splitting algorithm.

Hard constraint
---------------
Samples sharing the same ``group_id`` are atomic: a whole group is assigned to
exactly one split, so no group ever spans two splits ("group leakage" free).
A group may contain samples of several classes (e.g. one user's history); the
group's class-count vector is what gets allocated.

Soft objective
--------------
Within that constraint, keep each split's per-class counts as close as
possible to ``ratio * global_class_count``, and split sizes close to
``ratio * n``. Allocation is a greedy bin-packing that minimizes the marginal
increase in L1 deviation, processing groups largest-first in a canonical
order. When the targets cannot be met, the returned ``SplitDiagnostics``
reports both the measured deviation and its structural reason (a class
present in fewer groups than splits, oversized atomic groups, ...).

Determinism
-----------
The result depends only on the *content* of the data and the seed, never on
input row order: rows are aggregated into groups first, groups are processed
in a canonical order, and a seeded NumPy ``Generator`` supplies fixed
tie-breaking jitter.
"""
from __future__ import annotations

import math
from typing import Any, Hashable, Mapping, Sequence

import numpy as np

from .models import (
    ClassDeviation,
    GroupReport,
    SplitDiagnostics,
    SplitResult,
)

RATIO_SUM_TOLERANCE = 1e-9
# Jitter only breaks exact/near ties; real score gaps always dominate.
JITTER_SCALE = 1e-9
# Weight of the overall split-size objective vs. the per-class objective.
SIZE_WEIGHT = 0.5


def split_dataset(
    groups: Sequence[Hashable],
    labels: Sequence[Hashable],
    ratios: Sequence[float] | Mapping[str, float] = (0.7, 0.15, 0.15),
    *,
    split_names: Sequence[str] | None = None,
    seed: int = 42,
    tolerance: float = 0.05,
) -> SplitResult:
    """Split samples into disjoint, group-atomic, (softly) stratified splits.

    Parameters
    ----------
    groups:
        Group id per sample (length n). Samples with the same id are kept
        together in exactly one split.
    labels:
        Class label per sample (length n). A group may mix classes.
    ratios:
        Split ratios, either a sequence summing to 1.0 or an ordered mapping
        ``name -> ratio``.
    split_names:
        Names when ``ratios`` is a sequence. Defaults to
        ``("train", "val", "test")`` for three ratios, else ``split_0, ...``.
    seed:
        Fixed integer seed; identical (data, seed, ratios) always yields the
        identical split, independent of input row ordering.
    tolerance:
        Proportion band used for the pass/fail diagnostics (does not change
        the assignment algorithm itself).

    Returns
    -------
    SplitResult
    """
    names, ratio_values = _normalize_ratios(ratios, split_names)
    _validate_inputs(groups, labels, tolerance)

    groups = list(groups)
    labels = list(labels)
    n = len(groups)
    k = len(names)

    # --- Aggregate rows into atomic groups ---------------------------------
    # group id -> {"indices": [...], "class_counts": {label: count}}
    aggregated: dict[Hashable, dict[str, Any]] = {}
    class_counts: dict[Hashable, int] = {}
    for idx, (gid, label) in enumerate(zip(groups, labels)):
        entry = aggregated.get(gid)
        if entry is None:
            aggregated[gid] = {"indices": [idx], "class_counts": {label: 1}}
        else:
            entry["indices"].append(idx)
            cc = entry["class_counts"]
            cc[label] = cc.get(label, 0) + 1
        class_counts[label] = class_counts.get(label, 0) + 1

    classes = sorted(class_counts.keys(), key=str)

    # Canonical processing order: largest first, id as final tie-breaker.
    # Input row permutation therefore cannot affect the result.
    canonical_gids = sorted(
        aggregated.keys(), key=lambda g: (-len(aggregated[g]["indices"]), str(g))
    )
    group_sizes = {g: len(aggregated[g]["indices"]) for g in canonical_gids}

    # Targets.
    target_sizes = {s: ratio_values[i] * n for i, s in enumerate(names)}
    target_class = {
        s: {c: ratio_values[i] * class_counts[c] for c in classes}
        for i, s in enumerate(names)
    }

    # --- Greedy group assignment: minimize marginal L1 deviation ------------
    rng = np.random.default_rng(seed)
    jitter = rng.random(len(canonical_gids))

    cur_sizes = {s: 0 for s in names}
    cur_class = {s: {c: 0 for c in classes} for s in names}
    assignment: dict[Hashable, str] = {}

    for pos, gid in enumerate(canonical_gids):
        vec = aggregated[gid]["class_counts"]
        size = group_sizes[gid]
        deltas: dict[str, float] = {}
        for s in names:
            class_delta = 0.0
            for c, v in vec.items():
                before = abs(cur_class[s][c] - target_class[s][c])
                after = abs(cur_class[s][c] + v - target_class[s][c])
                class_delta += after - before
            size_before = abs(cur_sizes[s] - target_sizes[s])
            size_after = abs(cur_sizes[s] + size - target_sizes[s])
            # Smaller marginal deviation is better; negate for max-selection.
            deltas[s] = -(class_delta + SIZE_WEIGHT * (size_after - size_before))
            deltas[s] += float(jitter[pos]) * JITTER_SCALE
        chosen = max(names, key=lambda s: (deltas[s], -names.index(s)))
        assignment[gid] = chosen
        cur_sizes[chosen] += size
        for c, v in vec.items():
            cur_class[chosen][c] += v

    return _build_result(
        names=names,
        ratio_values=ratio_values,
        seed=seed,
        tolerance=tolerance,
        n=n,
        k=k,
        aggregated=aggregated,
        canonical_gids=canonical_gids,
        group_sizes=group_sizes,
        classes=classes,
        class_counts=class_counts,
        target_sizes=target_sizes,
        assignment=assignment,
        cur_sizes=cur_sizes,
        cur_class=cur_class,
    )


# --------------------------------------------------------------------------
# Validation / normalization
# --------------------------------------------------------------------------

def _normalize_ratios(
    ratios: Sequence[float] | Mapping[str, float],
    split_names: Sequence[str] | None,
) -> tuple[list[str], list[float]]:
    if isinstance(ratios, Mapping):
        names = [str(k) for k in ratios.keys()]
        values = [float(v) for v in ratios.values()]
    else:
        values = [float(v) for v in ratios]
        if split_names is not None:
            names = [str(v) for v in split_names]
        elif len(values) == 3:
            names = ["train", "val", "test"]
        else:
            names = [f"split_{i}" for i in range(len(values))]

    if len(names) < 1:
        raise ValueError("At least one split ratio is required")
    if len(set(names)) != len(names):
        raise ValueError(f"Split names must be unique, got {names!r}")
    if any(v <= 0 for v in values):
        raise ValueError(f"Split ratios must all be positive, got {values!r}")
    if not math.isclose(sum(values), 1.0, abs_tol=RATIO_SUM_TOLERANCE):
        raise ValueError(f"Split ratios must sum to 1.0, got sum={sum(values)!r}")
    return names, values


def _validate_inputs(
    groups: Sequence[Hashable],
    labels: Sequence[Hashable],
    tolerance: float,
) -> None:
    if len(groups) != len(labels):
        raise ValueError(
            f"groups and labels must have equal length, got {len(groups)} and {len(labels)}"
        )
    if len(groups) == 0:
        raise ValueError("Cannot split an empty dataset")
    if not 0.0 < tolerance < 1.0:
        raise ValueError(f"tolerance must be in (0, 1), got {tolerance!r}")
    # Fail fast on unhashable group/label values with a clear message.
    try:
        len({g: None for g in groups})
        len({l: None for l in labels})
    except TypeError as exc:
        raise ValueError("group ids and labels must be hashable") from exc


# --------------------------------------------------------------------------
# Result construction / diagnostics
# --------------------------------------------------------------------------

def _build_result(
    *,
    names: list[str],
    ratio_values: list[float],
    seed: int,
    tolerance: float,
    n: int,
    k: int,
    aggregated: Mapping[Hashable, dict[str, Any]],
    canonical_gids: list[Hashable],
    group_sizes: Mapping[Hashable, int],
    classes: list[Hashable],
    class_counts: Mapping[Hashable, int],
    target_sizes: Mapping[str, float],
    assignment: Mapping[Hashable, str],
    cur_sizes: Mapping[str, int],
    cur_class: Mapping[str, Mapping[Hashable, int]],
) -> SplitResult:
    assignments: dict[str, list[int]] = {s: [] for s in names}
    group_reports: list[GroupReport] = []
    for gid in canonical_gids:
        chosen = assignment[gid]
        indices = sorted(aggregated[gid]["indices"])
        assignments[chosen].extend(indices)
        vec = aggregated[gid]["class_counts"]
        dominant = max(vec, key=lambda c: (vec[c], str(c)))
        group_reports.append(
            GroupReport(
                group_id=str(gid),
                label=str(dominant),
                class_counts={str(c): int(v) for c, v in vec.items()},
                size=group_sizes[gid],
                split=chosen,
            )
        )
    for s in names:
        assignments[s].sort()

    # Per-class deviations.
    class_deviations: list[ClassDeviation] = []
    max_class_dev = 0.0
    for c in classes:
        split_counts = {s: cur_class[s][c] for s in names}
        split_proportions = {
            s: (split_counts[s] / cur_sizes[s] if cur_sizes[s] > 0 else 0.0)
            for s in names
        }
        global_prop = class_counts[c] / n
        dev = max(abs(split_proportions[s] - global_prop) for s in names)
        max_class_dev = max(max_class_dev, dev)
        class_deviations.append(
            ClassDeviation(
                label=str(c),
                global_count=class_counts[c],
                global_proportion=global_prop,
                split_counts=split_counts,
                split_proportions=split_proportions,
                max_abs_deviation=dev,
            )
        )

    max_count_dev = max(abs(cur_sizes[s] - target_sizes[s]) / n for s in names)

    # Structural conditions. A class appearing in fewer groups than splits
    # can never be present in every split (necessary combinatorial condition).
    groups_containing_class: dict[Hashable, int] = {}
    for gid in canonical_gids:
        for c in aggregated[gid]["class_counts"]:
            groups_containing_class[c] = groups_containing_class.get(c, 0) + 1

    rare_classes = [
        str(c) for c in classes if groups_containing_class[c] < k
    ]
    reasons: list[str] = []
    for c in classes:
        if groups_containing_class[c] < k:
            n_groups_c = groups_containing_class[c]
            missing = k - n_groups_c
            unavoidable = class_counts[c] / n
            within = "within" if max_class_dev <= tolerance else "outside"
            reasons.append(
                f"Class {c} appears in only {n_groups_c} group(s) but "
                f"there are {k} splits: at least {missing} split(s) must "
                f"contain 0 samples of it, so a proportion gap of up to "
                f"{unavoidable:.4f} is unavoidable "
                f"(observed max per-class deviation {max_class_dev:.4f}, "
                f"tolerance {tolerance} -> {within} tolerance)."
            )

    oversized = [
        gid
        for gid in canonical_gids
        if max_count_dev > tolerance and group_sizes[gid] / n > tolerance
    ]
    if max_count_dev > tolerance:
        if oversized:
            detail = ", ".join(
                f"{gid!r}({group_sizes[gid]}/{n}={group_sizes[gid] / n:.3f})"
                for gid in oversized[:10]
            )
            reasons.append(
                f"Split-size deviation {max_count_dev:.4f} exceeds tolerance "
                f"{tolerance}: groups are atomic and the largest group(s) "
                f"alone move a split by more than the tolerance band: {detail}."
            )
        else:
            reasons.append(
                f"Split-size deviation {max_count_dev:.4f} exceeds tolerance "
                f"{tolerance} due to the discrete (group-atomic) allocation; "
                f"no finer assignment exists without splitting a group."
            )

    if len(canonical_gids) < k:
        reasons.append(
            f"Only {len(canonical_gids)} group(s) exist for {k} splits: "
            f"{k - len(canonical_gids)} split(s) must be empty."
        )

    diagnostics = SplitDiagnostics(
        tolerance=tolerance,
        stratified_within_tolerance=max_class_dev <= tolerance,
        counts_within_tolerance=max_count_dev <= tolerance,
        max_class_proportion_deviation=max_class_dev,
        max_split_count_deviation=max_count_dev,
        reasons=reasons,
        rare_classes=rare_classes,
        oversized_groups=[str(g) for g in oversized],
    )

    return SplitResult(
        split_names=names,
        ratios={s: ratio_values[i] for i, s in enumerate(names)},
        seed=seed,
        n_samples=n,
        n_groups=len(canonical_gids),
        split_sizes={s: cur_sizes[s] for s in names},
        assignments=assignments,
        group_assignments={str(g): assignment[g] for g in canonical_gids},
        group_reports=group_reports,
        class_deviations=class_deviations,
        diagnostics=diagnostics,
    )
