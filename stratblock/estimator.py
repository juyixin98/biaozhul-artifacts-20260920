"""Estimation kernel.

Computes treatment-effect estimates from recorded outcomes:

* difference in group means
* Welch (unequal-variance) two-sample t statistic, p-value and CI
* exact/Monte-Carlo label-permutation test (SciPy)

Crucially every report carries ``proves_allocation_correct = False`` with an
explicit reason: post-allocation significance is evidence about the effect
estimate, never about the correctness of the randomisation. Allocation
correctness is established separately in :mod:`stratblock.diagnostics`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy import stats

# Above this many label subsets, a Monte-Carlo approximation is used.
EXACT_ENUMERATION_LIMIT = 20_000
DEFAULT_PERMUTATION_REPS = 10_000
RNG_SEED_NOTE = "numpy default_rng, seed recorded per report"


@dataclass(frozen=True)
class GroupData:
    arm: str
    values: tuple[float, ...]


def _as_group(arm: str, values: list[float]) -> GroupData:
    clean = [float(v) for v in values]
    if not clean:
        raise ValueError(f"arm {arm!r} has no observed outcomes")
    return GroupData(arm=arm, values=tuple(clean))


def difference_in_means(a: GroupData, b: GroupData) -> float:
    return float(np.mean(a.values) - np.mean(b.values))


def welch_test(a: GroupData, b: GroupData) -> dict[str, float]:
    """Welch two-sample test (a minus b) with a 95% CI for the mean diff."""
    x = np.asarray(a.values, dtype=float)
    y = np.asarray(b.values, dtype=float)
    n1, n2 = len(x), len(y)
    if n1 < 2 or n2 < 2:
        raise ValueError("Welch test requires at least 2 observations per arm")
    m1, m2 = float(np.mean(x)), float(np.mean(y))
    v1, v2 = float(np.var(x, ddof=1)), float(np.var(y, ddof=1))
    se = math.sqrt(v1 / n1 + v2 / n2)
    if se == 0.0:
        raise ValueError("zero standard error (both groups constant)")
    t_stat = (m1 - m2) / se
    df_num = (v1 / n1 + v2 / n2) ** 2
    df_den = (v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1)
    df = df_num / df_den
    p_value = float(2.0 * stats.t.sf(abs(t_stat), df))
    crit = float(stats.t.ppf(0.975, df))
    return {
        "estimate": m1 - m2,
        "se": se,
        "t_statistic": float(t_stat),
        "df": float(df),
        "p_value": p_value,
        "ci95_low": (m1 - m2) - crit * se,
        "ci95_high": (m1 - m2) + crit * se,
    }


def permutation_test(
    a: GroupData,
    b: GroupData,
    reps: int = DEFAULT_PERMUTATION_REPS,
    seed: int = 0,
) -> dict[str, Any]:
    """Two-sided label-permutation test on the difference in means.

    Uses the exact enumeration only when ``C(n1+n2, n1)`` is below
    :data:`EXACT_ENUMERATION_LIMIT`; otherwise a fixed-seed Monte-Carlo
    approximation whose seed is recorded in the result.
    """
    x = np.asarray(a.values, dtype=float)
    y = np.asarray(b.values, dtype=float)
    pooled = np.concatenate([x, y])
    n1 = len(x)
    n_total = len(pooled)
    observed = float(np.mean(pooled[:n1]) - np.mean(pooled[n1:]))
    n_subsets = math.comb(n_total, n1)

    if n_subsets <= EXACT_ENUMERATION_LIMIT:
        rng = np.random.default_rng(seed)
        count = 0
        # Exact: enumerate all n1-subsets (SciPy has no direct exact routine
        # for arbitrary sizes; the bound keeps this tractable and verifiable).
        from itertools import combinations

        null_dist: list[float] = []
        for chosen in combinations(range(n_total), n1):
            mask = np.zeros(n_total, dtype=bool)
            mask[list(chosen)] = True
            stat = float(pooled[mask].mean() - pooled[~mask].mean())
            null_dist.append(stat)
            if abs(stat) >= abs(observed) - 1e-12:
                count += 1
        p_value = count / n_subsets
        method = "exact_enumeration"
        null_values = np.asarray(null_dist)
        rng_note = "no RNG used for exact enumeration"
    else:
        rng = np.random.default_rng(seed)
        count = 1  # include observed permutation
        values = pooled.copy()
        for _ in range(reps):
            rng.shuffle(values)
            stat = float(values[:n1].mean() - values[n1:].mean())
            if abs(stat) >= abs(observed) - 1e-12:
                count += 1
        p_value = count / (reps + 1)
        method = "monte_carlo"
        null_values = None
        rng_note = f"numpy default_rng(seed={seed}), {reps} shuffles"

    result: dict[str, Any] = {
        "method": method,
        "estimate": observed,
        "p_value": float(p_value),
        "n_permutations": int(n_subsets if method == "exact_enumeration" else reps + 1),
        "rng_note": rng_note,
        "null_mean": float(np.mean(null_dist)) if method == "exact_enumeration" else None,
        "null_sd": float(np.std(np.asarray(null_dist), ddof=1))
        if method == "exact_enumeration" and n_subsets > 1
        else None,
    }
    return result


def build_effect_report(
    arm_a: str,
    values_a: list[float],
    arm_b: str,
    values_b: list[float],
    permutation_seed: int = 0,
) -> dict[str, Any]:
    """Full estimation report with the scope limitation stated explicitly."""
    ga = _as_group(arm_a, values_a)
    gb = _as_group(arm_b, values_b)
    welch: dict[str, Any]
    try:
        welch = welch_test(ga, gb)
        welch["status"] = "ok"
    except ValueError as exc:
        welch = {"status": "indeterminate", "reason": str(exc)}
    try:
        perm = permutation_test(ga, gb, seed=permutation_seed)
        perm["status"] = "ok"
    except ValueError as exc:
        perm = {"status": "indeterminate", "reason": str(exc)}
    return {
        "comparison": {"arm_a": arm_a, "arm_b": arm_b},
        "n": {arm_a: len(ga.values), arm_b: len(gb.values)},
        "means": {arm_a: float(np.mean(ga.values)), arm_b: float(np.mean(gb.values))},
        "difference_in_means": difference_in_means(ga, gb),
        "welch": welch,
        "permutation_test": perm,
        "interpretation_scope": (
            "These statistics describe the observed between-arm difference. "
            "A significant p-value is NOT evidence that allocation was correct; "
            "allocation correctness is verified via the diagnostics endpoint "
            "(block counts, stream replay, reference-oracle agreement)."
        ),
        "proves_allocation_correct": False,
        "proves_allocation_correct_reason": (
            "post-allocation outcome tests cannot detect a broken or biased "
            "randomisation with certainty; they address estimands, not mechanism"
        ),
    }
