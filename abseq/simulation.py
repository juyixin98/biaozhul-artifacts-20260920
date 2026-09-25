"""Simulation harness: interval coverage and sequential-peeking bias.

`coverage_simulation` estimates the empirical coverage of the fixed-horizon
Welch interval under a known true effect (0.0 = no difference), across
missing-value strategies and arbitrarily imbalanced group sizes.

`peeking_type1_simulation` demonstrates *why* this package refuses to
support peek-and-stop: repeatedly testing accumulating data at the nominal
level inflates the type-I error rate well beyond the nominal alpha.
"""

from __future__ import annotations

import numpy as np

from .analysis import analyze
from .stats import normal_ppf

DEFAULT_REPS = 2000


def coverage_simulation(
    n_control: int,
    n_treatment: int,
    effect: float = 0.0,
    reps: int = DEFAULT_REPS,
    confidence: float = 0.95,
    missing_strategy: str = "drop",
    missing_rate: float = 0.0,
    seed: int = 0,
    control_mean: float = 10.0,
    sd: float = 2.0,
) -> dict:
    """Estimate empirical coverage of the Welch interval by Monte Carlo.

    Each replication draws fresh normal data with the given true effect,
    runs the fixed-horizon analysis, and records whether the interval
    contains the true difference. Replications that become degenerate
    after missing-value handling (fewer than 2 observations in a group)
    are skipped and counted separately.
    """
    if reps < 1:
        raise ValueError(f"reps must be positive, got {reps!r}")
    rng = np.random.default_rng(seed)
    covered = 0
    widths: list[float] = []
    skipped = 0
    for _ in range(reps):
        rep_seed = int(rng.integers(0, 2**31 - 1))
        rep_rng = np.random.default_rng(rep_seed)
        control = rep_rng.normal(control_mean, sd, size=n_control)
        treatment = rep_rng.normal(control_mean + effect, sd, size=n_treatment)
        if missing_rate > 0.0:
            total = n_control + n_treatment
            mask = rep_rng.random(total) < missing_rate
            values = np.concatenate([control, treatment])
            values[mask] = np.nan
            control, treatment = values[:n_control], values[n_control:]
        try:
            result = analyze(
                control, treatment,
                confidence=confidence, missing_strategy=missing_strategy,
            )
        except ValueError:
            skipped += 1
            continue
        if result["ci_lower"] <= effect <= result["ci_upper"]:
            covered += 1
        widths.append(result["ci_upper"] - result["ci_lower"])
    valid = reps - skipped
    return {
        "n_control": n_control,
        "n_treatment": n_treatment,
        "true_effect": effect,
        "reps": reps,
        "skipped_degenerate_reps": skipped,
        "confidence_level": confidence,
        "missing_strategy": missing_strategy,
        "missing_rate": missing_rate,
        "seed": seed,
        "empirical_coverage": covered / valid if valid else None,
        "mean_interval_width": float(np.mean(widths)) if widths else None,
    }


def peeking_type1_simulation(
    n_per_group: int,
    looks: int = 5,
    reps: int = DEFAULT_REPS,
    alpha: float = 0.05,
    seed: int = 0,
    control_mean: float = 10.0,
    sd: float = 2.0,
) -> dict:
    """Show type-I error inflation from repeatedly peeking under the null.

    Data are generated with zero true effect. The experiment is "monitored"
    at `looks` equally spaced horizons; at each look a two-sided z-test at
    level `alpha` is applied to the data accumulated so far. The reported
    `ever_significant_rate` is the probability of at least one nominal
    rejection, which a valid fixed-horizon procedure would cap at `alpha`.
    """
    if looks < 1 or n_per_group < 2:
        raise ValueError("need looks >= 1 and n_per_group >= 2")
    rng = np.random.default_rng(seed)
    crit = normal_ppf(1.0 - alpha / 2.0)
    horizons = [
        max(2, int(round(n_per_group * k / looks))) for k in range(1, looks + 1)
    ]
    horizons[-1] = n_per_group
    ever_significant = 0
    for _ in range(reps):
        control = rng.normal(control_mean, sd, size=n_per_group)
        treatment = rng.normal(control_mean, sd, size=n_per_group)
        for horizon in horizons:
            c, t = control[:horizon], treatment[:horizon]
            se = np.sqrt(np.var(c, ddof=1) / horizon + np.var(t, ddof=1) / horizon)
            if se > 0 and abs(np.mean(t) - np.mean(c)) / se > crit:
                ever_significant += 1
                break
    return {
        "n_per_group": n_per_group,
        "looks": looks,
        "reps": reps,
        "nominal_alpha": alpha,
        "seed": seed,
        "ever_significant_rate": ever_significant / reps,
        "interpretation": (
            "Under a true null, peeking "
            f"{looks} times at accumulating data raises the probability of "
            "at least one nominal rejection above the nominal alpha; "
            "fixed-horizon intervals do not protect against this."
        ),
    }
