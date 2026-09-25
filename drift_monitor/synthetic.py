"""Reproducible synthetic data for drift experiments.

No external data is downloaded. A seeded generator builds a small
tabular binary-classification dataset with several numeric features;
scenario presets mutate the *current* window relative to a stable
baseline so drift behavior can be checked deterministically.

Scenarios
----------
``same``          identical joint distribution (expect low PSI)
``mean_shift``    additive location shift on informative features (expect high PSI)
``scale_change``  one feature's variance changes
``missing_spike`` missingness jumps from ~2% to ~40%
``small_sample``  current window shrunk to a handful of rows
``all_missing``   one feature is NaN everywhere in the current window
``mixed``         mean shift + missing spike combined
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SCENARIOS = (
    "same", "mean_shift", "scale_change", "missing_spike",
    "small_sample", "all_missing", "mixed",
)
FEATURE_NAMES = ["income", "age_z", "tx_count", "risk_score"]


@dataclass(frozen=True)
class Dataset:
    X_base: dict[str, np.ndarray]
    X_cur: dict[str, np.ndarray]
    y_base: np.ndarray
    y_cur: np.ndarray
    scenario: str
    seed: int

    @property
    def n_baseline(self) -> int:
        return self.y_base.size

    @property
    def n_current(self) -> int:
        return self.y_cur.size


def _sample_core(rng: np.random.Generator, n: int) -> dict[str, np.ndarray]:
    """Draw a raw feature table in its *baseline* regime."""
    income = rng.lognormal(mean=3.5, sigma=0.6, size=n)          # skewed > 0
    age_z = rng.normal(loc=0.0, scale=1.0, size=n)               # standard normal
    tx_count = rng.poisson(lam=8.0, size=n).astype(np.float64)   # discrete-ish
    # risk_score depends on the other features + noise, creates correlation
    risk_score = (
        0.01 * income + 0.8 * age_z - 0.15 * tx_count
        + rng.normal(0.0, 1.0, size=n)
    )
    return {
        "income": income,
        "age_z": age_z,
        "tx_count": tx_count,
        "risk_score": risk_score,
    }


def _logit(X: np.ndarray) -> np.ndarray:
    z = X @ np.array([0.004, 0.9, -0.12, 0.5]) - 1.5
    return 1.0 / (1.0 + np.exp(-z))


def _labels(x: dict[str, np.ndarray], rng: np.random.Generator) -> np.ndarray:
    X = np.column_stack([x[name] for name in FEATURE_NAMES])
    p = _logit(X)
    return (rng.random(size=p.size) < p).astype(np.int64)


def _inject_missing(x: dict[str, np.ndarray], rng: np.random.Generator,
                    rate: float, features: tuple[str, ...]) -> None:
    for name in features:
        col = x[name].copy()
        col[rng.random(size=col.size) < rate] = np.nan
        x[name] = col


def make_dataset(scenario: str = "same", n_baseline: int = 5000,
                 n_current: int = 2000, seed: int = 42) -> Dataset:
    """Generate a paired baseline/current dataset for a scenario.

    Two independent RNG streams keep the baseline identical across
    scenarios (only the scenario changes), which makes comparisons clean.
    """
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}; choose from {SCENARIOS}")
    if n_baseline < 1 or n_current < 1:
        raise ValueError("window sizes must be >= 1")

    base_rng = np.random.default_rng(seed)
    cur_rng = np.random.default_rng(seed + 10_000)

    X_base = _sample_core(base_rng, n_baseline)
    X_cur = _sample_core(cur_rng, n_current)

    # Covariate drift mutates P(x) only; the label rule P(y|x) is held
    # constant, so location/scale changes are applied before drawing the
    # current labels.
    if scenario in ("mean_shift", "mixed"):
        X_cur["income"] = X_cur["income"] * 1.6 + 200.0
        X_cur["age_z"] = X_cur["age_z"] + 1.2
        X_cur["risk_score"] = X_cur["risk_score"] + 1.5
    if scenario == "scale_change":
        X_cur["tx_count"] = X_cur["tx_count"] * 3.0

    # Labels depend on the *latent* feature values, so draw them before
    # missingness is injected; otherwise an all-missing column would poison
    # the logit with NaN and collapse every label to 0.
    y_base = _labels(X_base, np.random.default_rng(seed + 20_000))
    y_cur = _labels(X_cur, np.random.default_rng(seed + 30_000))

    _inject_missing(X_base, base_rng, 0.02, ("income", "risk_score"))
    _inject_missing(X_cur, cur_rng, 0.02, ("income", "risk_score"))
    if scenario in ("missing_spike", "mixed"):
        _inject_missing(X_cur, cur_rng, 0.40, ("income",))
    if scenario == "all_missing":
        X_cur["risk_score"] = np.full(n_current, np.nan)
    if scenario == "small_sample":
        keep = 12
        X_cur = {k: v[:keep].copy() for k, v in X_cur.items()}
        y_cur = y_cur[:keep]

    return Dataset(
        X_base=X_base, X_cur=X_cur, y_base=y_base, y_cur=y_cur,
        scenario=scenario, seed=seed,
    )
