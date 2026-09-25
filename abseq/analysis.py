"""Fixed-sample-size A/B metric analysis.

The public entry points are `analyze` (two explicit samples) and
`analyze_groups` (group labels + pooled observations). Both are strictly
fixed-horizon: the confidence interval is only valid when the sample size
was fixed before looking at the data. Every result carries an explicit
validity warning to that effect.
"""

from __future__ import annotations

import numpy as np

from .missing import apply_missing_strategy
from .stats import welch_t_interval

VALIDITY_WARNING = (
    "Fixed-horizon analysis only: the confidence interval is valid solely "
    "when the sample size was fixed before the data were examined. "
    "Repeatedly peeking at results and stopping early invalidates the "
    "nominal coverage; this tool does not support sequential monitoring."
)

ASSUMPTIONS = [
    "Observations are independent within and between groups.",
    "Sample sizes were fixed in advance (no optional stopping).",
    "Welch t-interval: approximately valid for moderate/large samples; "
    "exact only under per-group normality.",
]


def analyze(
    control,
    treatment,
    confidence: float = 0.95,
    missing_strategy: str = "raise",
) -> dict:
    """Analyze a fixed-sample A/B comparison of two observation vectors."""
    control = np.asarray(control, dtype=float)
    treatment = np.asarray(treatment, dtype=float)
    clean_c, miss_c = apply_missing_strategy(control, missing_strategy, "control")
    clean_t, miss_t = apply_missing_strategy(treatment, missing_strategy, "treatment")

    interval = welch_t_interval(clean_c, clean_t, confidence)
    result = {
        "n_control": int(clean_c.size),
        "n_treatment": int(clean_t.size),
        "n_missing_control": miss_c,
        "n_missing_treatment": miss_t,
        "missing_strategy": missing_strategy,
        **interval,
        "assumptions": ASSUMPTIONS,
        "validity_warning": VALIDITY_WARNING,
    }
    if missing_strategy == "impute_mean" and (miss_c or miss_t):
        result["imputation_note"] = (
            "Missing values were imputed with the observed group mean; this "
            "understates within-group variance and narrows the interval. "
            "Prefer 'drop' unless this bias is acceptable."
        )
    return result


def analyze_groups(
    groups,
    observations,
    confidence: float = 0.95,
    missing_strategy: str = "raise",
) -> dict:
    """Analyze pooled observations tagged with exactly two group labels.

    The difference is reported as mean(treatment_label) - mean(control_label)
    where the labels are the two distinct sorted label values; the labels
    used are echoed in the result.
    """
    groups = np.asarray(groups)
    observations = np.asarray(observations, dtype=float)
    if groups.shape[0] != observations.shape[0]:
        raise ValueError(
            f"groups and observations must have equal length, got "
            f"{groups.shape[0]} and {observations.shape[0]}"
        )
    labels = np.unique(groups)
    if labels.size != 2:
        raise ValueError(
            f"expected exactly 2 distinct group labels, got {labels.tolist()}"
        )
    control_label, treatment_label = (str(labels[0]), str(labels[1]))
    result = analyze(
        observations[groups == labels[0]],
        observations[groups == labels[1]],
        confidence=confidence,
        missing_strategy=missing_strategy,
    )
    result["control_label"] = control_label
    result["treatment_label"] = treatment_label
    return result
