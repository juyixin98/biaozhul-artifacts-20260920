"""Reproducible synthetic data generation (no external data or models).

All randomness flows through numpy.random.Generator seeded by the caller,
so every dataset and simulation in this project is exactly reproducible.
"""

from __future__ import annotations

import numpy as np

DEFAULT_CONTROL_MEAN = 10.0
DEFAULT_SD = 2.0


def make_synthetic(
    n_control: int,
    n_treatment: int,
    effect: float = 0.0,
    seed: int = 0,
    missing_rate: float = 0.0,
    control_mean: float = DEFAULT_CONTROL_MEAN,
    sd: float = DEFAULT_SD,
) -> tuple[np.ndarray, np.ndarray]:
    """Generate a synthetic two-group dataset.

    Observations are i.i.d. Normal(control_mean, sd) for control and
    Normal(control_mean + effect, sd) for treatment. A uniform mask then
    sets a fraction `missing_rate` of all observations to NaN (missing
    completely at random). Returns (groups, observations) with labels
    "control"/"treatment".
    """
    if n_control < 1 or n_treatment < 1:
        raise ValueError("group sizes must be positive")
    if not 0.0 <= missing_rate < 1.0:
        raise ValueError(f"missing_rate must be in [0, 1), got {missing_rate!r}")
    if sd <= 0:
        raise ValueError(f"sd must be positive, got {sd!r}")

    rng = np.random.default_rng(seed)
    control = rng.normal(control_mean, sd, size=n_control)
    treatment = rng.normal(control_mean + effect, sd, size=n_treatment)

    groups = np.array(["control"] * n_control + ["treatment"] * n_treatment)
    observations = np.concatenate([control, treatment])
    if missing_rate > 0.0:
        mask = rng.random(observations.size) < missing_rate
        observations[mask] = np.nan
    return groups, observations
