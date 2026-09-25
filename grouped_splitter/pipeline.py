"""End-to-end local pipeline: synthetic data -> grouped split -> checks -> model.

Everything here is offline and deterministic. ``run_synthetic_demo`` returns a
plain ``dict`` report that is JSON-serializable and also powers the HTTP
service's ``/demo`` endpoint and the ``scripts/demo.py`` command.
"""
from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from .model import LogisticRegression
from .splitter import split_dataset
from .synthetic import Dataset, generate_synthetic_dataset


def run_synthetic_demo(
    *,
    n_samples: int = 2000,
    n_features: int = 8,
    n_classes: int = 3,
    n_groups: int = 40,
    ratios: tuple[float, ...] | Mapping[str, float] = (0.7, 0.15, 0.15),
    seed: int = 42,
    tolerance: float = 0.05,
    train_model: bool = True,
) -> dict[str, Any]:
    """Generate data, split it, run all acceptance checks, optionally train."""
    dataset = generate_synthetic_dataset(
        n_samples=n_samples,
        n_features=n_features,
        n_classes=n_classes,
        n_groups=n_groups,
        seed=seed,
    )
    result = split_dataset(
        groups=list(dataset.group_ids),
        labels=list(dataset.y),
        ratios=ratios,
        seed=seed,
        tolerance=tolerance,
    )

    checks = verify_invariants(dataset, result)
    report: dict[str, Any] = {
        "dataset": {
            "n_samples": dataset.n_samples,
            "n_features": dataset.n_features,
            "n_classes": n_classes,
            "n_groups": dataset.n_groups,
            "class_names": list(dataset.class_names),
            "global_class_counts": _class_count_table(dataset.y),
            "group_size_summary": _group_size_summary(dataset),
        },
        "split": _split_report(result),
        "checks": checks,
    }
    if train_model:
        report["model"] = _train_and_evaluate(dataset, result)
    return report


def verify_invariants(dataset: Dataset, result: Any) -> dict[str, Any]:
    """Independently re-check group isolation, conservation and coverage."""
    groups = dataset.group_ids
    y = dataset.y
    n = dataset.n_samples
    split_sets = {s: set(idx) for s, idx in result.assignments.items()}

    # 1) Conservation: every sample assigned exactly once, nothing lost/dup.
    all_assigned: list[int] = []
    for s in result.split_names:
        all_assigned.extend(result.assignments[s])
    total_conserved = sorted(all_assigned) == list(range(n)) and len(all_assigned) == n

    # 2) Pairwise disjointness.
    disjoint = True
    names = result.split_names
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            if split_sets[names[i]] & split_sets[names[j]]:
                disjoint = False

    # 3) Group isolation: no group id appears in two splits.
    group_to_splits: dict[str, set[str]] = {}
    leakage_groups: list[str] = []
    for s in names:
        for idx in result.assignments[s]:
            gid = str(groups[idx])
            seen = group_to_splits.setdefault(gid, set())
            if seen and s not in seen:
                leakage_groups.append(gid)
            seen.add(s)
    group_isolated = not leakage_groups
    all_groups_assigned = set(group_to_splits) == set(map(str, np.unique(groups)))

    # 4) Reported sizes match the actual assignment lists.
    sizes_consistent = all(
        result.split_sizes[s] == len(result.assignments[s]) for s in names
    )

    # 5) Recompute per-class proportions directly from the raw arrays.
    per_split_class = {}
    for s in names:
        idx = np.asarray(sorted(result.assignments[s]), dtype=np.int64)
        if idx.size:
            vals, cnt = np.unique(y[idx], return_counts=True)
            per_split_class[s] = {str(int(v)): int(c) for v, c in zip(vals, cnt)}
        else:
            per_split_class[s] = {}

    return {
        "total_samples_conserved": total_conserved,
        "splits_pairwise_disjoint": disjoint,
        "groups_isolated_to_one_split": group_isolated,
        "all_groups_assigned": all_groups_assigned,
        "reported_sizes_consistent": sizes_consistent,
        "leaked_groups": sorted(set(leakage_groups)),
        "per_split_class_counts": per_split_class,
    }


# --------------------------------------------------------------------------
# Reporting helpers
# --------------------------------------------------------------------------

def _split_report(result: Any) -> dict[str, Any]:
    return {
        "split_names": result.split_names,
        "ratios": result.ratios,
        "seed": result.seed,
        "tolerance": result.diagnostics.tolerance,
        "split_sizes": result.split_sizes,
        "stratified_within_tolerance":
            result.diagnostics.stratified_within_tolerance,
        "counts_within_tolerance": result.diagnostics.counts_within_tolerance,
        "max_class_proportion_deviation":
            result.diagnostics.max_class_proportion_deviation,
        "max_split_count_deviation":
            result.diagnostics.max_split_count_deviation,
        "rare_classes": result.diagnostics.rare_classes,
        "oversized_groups": result.diagnostics.oversized_groups,
        "reasons": result.diagnostics.reasons,
        "class_deviations": [
            {
                "label": cd.label,
                "global_proportion": cd.global_proportion,
                "split_proportions": cd.split_proportions,
                "split_counts": cd.split_counts,
                "max_abs_deviation": cd.max_abs_deviation,
            }
            for cd in result.class_deviations
        ],
        "n_groups_per_split": {
            s: sum(1 for g in result.group_assignments.values() if g == s)
            for s in result.split_names
        },
        # Full assignment is large; exposed via the service only on demand.
    }


def _class_count_table(y: np.ndarray) -> dict[str, int]:
    vals, cnt = np.unique(y, return_counts=True)
    return {str(int(v)): int(c) for v, c in zip(vals, cnt)}


def _group_size_summary(dataset: Dataset) -> dict[str, Any]:
    table = dataset.group_size_table()
    sizes = np.asarray(list(table.values()), dtype=np.int64)
    return {
        "n_groups": int(sizes.size),
        "min": int(sizes.min()),
        "max": int(sizes.max()),
        "mean": float(sizes.mean()),
        "largest_groups": dict(
            sorted(table.items(), key=lambda kv: -kv[1])[:5]
        ),
    }


def _train_and_evaluate(dataset: Dataset, result: Any) -> dict[str, Any]:
    """Train on ``train``; evaluate on every non-empty split.

    Rare classes that structurally cannot reach val/test are reported
    separately rather than treated as a model failure.
    """
    names = result.split_names
    idx = {s: np.asarray(sorted(result.assignments[s]), dtype=np.int64)
           for s in names}
    train_name = names[0]
    model = LogisticRegression(seed=result.seed).fit(
        dataset.X[idx[train_name]], dataset.y[idx[train_name]]
    )

    metrics: dict[str, Any] = {}
    for s in names:
        if idx[s].size == 0:
            metrics[s] = {"n": 0, "accuracy": None, "note": "empty split"}
            continue
        present = sorted({int(v) for v in np.unique(dataset.y[idx[s]])})
        note = None
        if s != train_name:
            missing = sorted(
                c for c in range(len(dataset.class_names)) if c not in present
            )
            if missing:
                note = (
                    f"classes absent in this split for structural reasons "
                    f"(fewer groups than splits): {missing}"
                )
        metrics[s] = {
            "n": int(idx[s].size),
            "accuracy": model.accuracy(dataset.X[idx[s]], dataset.y[idx[s]]),
            "classes_present": present,
            "note": note,
        }
    return {
        "type": "numpy_multinomial_logistic_regression",
        "n_epochs": len(model.loss_history),
        "initial_loss": model.loss_history[0],
        "final_loss": model.loss_history[-1],
        "metrics": metrics,
    }
