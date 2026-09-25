"""Coverage validation of the Welch interval via reproducible simulation.

Under the null hypothesis (no true difference), a correct (1 - alpha)
confidence interval must contain the true difference (0) in roughly
(1 - alpha) of repeated experiments. This module runs that check on
synthetic data with a fixed seed — no external data or models involved.

Scenarios covered:
- balanced groups, no missing data;
- severely imbalanced group sizes;
- missing observations under each missing-value strategy.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from .stats import welch_mean_diff_ci

DEFAULT_SEED = 20260922


@dataclass(frozen=True)
class CoverageResult:
    scenario: str
    n_simulations: int
    confidence_level: float
    n_control: int
    n_treatment: int
    missing_rate: float
    missing_strategy: str
    seed: int
    coverage: float  # fraction of intervals containing the true diff (0)
    mean_ci_width: float

    def to_dict(self) -> dict:
        return asdict(self)


def estimate_coverage(
    n_simulations: int = 2000,
    confidence_level: float = 0.95,
    n_control: int = 500,
    n_treatment: int = 500,
    missing_rate: float = 0.0,
    missing_strategy: str = "drop",
    seed: int = DEFAULT_SEED,
    scenario: str = "custom",
) -> CoverageResult:
    """Simulate A/A experiments (true difference = 0) and measure how often
    the Welch interval contains 0.

    Both groups are drawn from the same standard normal distribution, so the
    true mean difference is exactly 0. ``missing_rate`` independently turns
    each observation into NaN before analysis.
    """
    if n_simulations < 1:
        raise ValueError("n_simulations must be >= 1")
    if not 0.0 <= missing_rate < 1.0:
        raise ValueError(f"missing_rate must be in [0, 1), got {missing_rate}")

    rng = np.random.default_rng(seed)
    hits = 0
    width_sum = 0.0
    for _ in range(n_simulations):
        control = rng.standard_normal(n_control)
        treatment = rng.standard_normal(n_treatment)
        if missing_rate > 0.0:
            control = control.copy()
            treatment = treatment.copy()
            control[rng.random(n_control) < missing_rate] = np.nan
            treatment[rng.random(n_treatment) < missing_rate] = np.nan
        result = welch_mean_diff_ci(
            control,
            treatment,
            confidence_level=confidence_level,
            missing_strategy=missing_strategy,
        )
        if result.ci_lower <= 0.0 <= result.ci_upper:
            hits += 1
        width_sum += result.ci_upper - result.ci_lower

    return CoverageResult(
        scenario=scenario,
        n_simulations=n_simulations,
        confidence_level=confidence_level,
        n_control=n_control,
        n_treatment=n_treatment,
        missing_rate=missing_rate,
        missing_strategy=missing_strategy,
        seed=seed,
        coverage=hits / n_simulations,
        mean_ci_width=width_sum / n_simulations,
    )


def run_standard_scenarios(
    n_simulations: int = 2000,
    confidence_level: float = 0.95,
    seed: int = DEFAULT_SEED,
) -> list[CoverageResult]:
    """Run the acceptance-test scenario matrix with a fixed seed."""
    return [
        estimate_coverage(
            n_simulations=n_simulations,
            confidence_level=confidence_level,
            n_control=500,
            n_treatment=500,
            seed=seed,
            scenario="balanced_no_missing",
        ),
        estimate_coverage(
            n_simulations=n_simulations,
            confidence_level=confidence_level,
            n_control=50,
            n_treatment=5000,
            seed=seed,
            scenario="imbalanced_1_to_100",
        ),
        estimate_coverage(
            n_simulations=n_simulations,
            confidence_level=confidence_level,
            n_control=500,
            n_treatment=500,
            missing_rate=0.2,
            missing_strategy="drop",
            seed=seed,
            scenario="missing_20pct_drop",
        ),
        estimate_coverage(
            n_simulations=n_simulations,
            confidence_level=confidence_level,
            n_control=500,
            n_treatment=500,
            missing_rate=0.2,
            missing_strategy="impute_mean",
            seed=seed,
            scenario="missing_20pct_impute_mean",
        ),
    ]
