"""Synthetic data generation with a KNOWN, documented data-generating process.

Everything is local and deterministic given a seed. The generator exposes the
true propensity and outcome functions so experiments can check coverage and
bias against ground truth — reference answers are computed independently of
the estimator under test.

Scenarios:

* ``good_overlap``   : linear logit propensity bounded away from 0/1;
* ``poor_overlap``   : strong covariate shift, scores pile near the boundary;
* ``no_overlap``     : near-deterministic threshold assignment with a handful of
                       deep "crossover" contaminant units. Out-of-fold models
                       fit extreme logits for those units, so scores reach the
                       numerical 0/1 boundary and the positivity guard fires —
                       a reproducible empirical positivity violation, not a
                       mere lack of intersection.
* ``misspecification``: true propensity is strongly nonlinear (X1*X2, X1^2),
                       while the fitted model is the linear logistic ridge —
                       calibration diagnostics should flag miscalibration.

Outcome model (shared): Y = mu_a(X) + eps, with
    mu_1(X) - mu_0(X) = tau(X) = tau_const + tau_x * X1
so the population ATE over any covariate distribution is reported by the
generator as the empirical mean of tau(X) over the DRAWN sample (and the
theoretical mean over the generating distribution where available).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

Scenario = Literal["good_overlap", "poor_overlap", "no_overlap", "misspecification"]


@dataclass(frozen=True)
class SyntheticDataset:
    x: np.ndarray
    a: np.ndarray
    y: np.ndarray
    p_true: np.ndarray
    tau: np.ndarray
    ate_true_sample: float
    scenario: str
    seed: int
    coef: dict[str, float]


def true_propensity(x: np.ndarray, scenario: Scenario, coef: dict[str, float]) -> np.ndarray:
    x1 = x[:, 0]
    x2 = x[:, 1] if x.shape[1] > 1 else np.zeros(len(x))
    if scenario == "good_overlap":
        eta = coef["a0"] + coef["a1"] * x1 + coef["a2"] * x2
    elif scenario == "poor_overlap":
        eta = coef["a0"] + coef["poor_mult"] * (coef["a1"] * x1 + coef["a2"] * x2)
    elif scenario == "no_overlap":
        eta = coef["sep_mult"] * (x1 - coef["sep_threshold"])
    elif scenario == "misspecification":
        eta = (
            coef["a0"]
            + coef["miss_inter"] * x1 * x2
            + coef["miss_sq"] * x1**2
            + coef["a2"] * x2
        )
    else:
        raise ValueError(f"unknown scenario {scenario!r}")
    return 1.0 / (1.0 + np.exp(-np.clip(eta, -60, 60)))


def generate_synthetic(
    n: int = 2000,
    *,
    scenario: Scenario = "good_overlap",
    seed: int = 42,
    n_covariates: int = 3,
    noise_sd: float = 1.0,
    tau_const: float = 2.0,
    tau_x: float = 0.5,
) -> SyntheticDataset:
    if n_covariates < 2:
        raise ValueError("n_covariates must be >= 2 for the declared scenarios")
    rng = np.random.default_rng(seed)
    x = rng.normal(0.0, 1.0, size=(n, n_covariates))

    coef = {
        "a0": 0.2,
        "a1": 0.8,
        "a2": -0.5,
        "poor_mult": 3.5,
        "sep_mult": 8.0,
        "sep_threshold": 0.0,
        "miss_inter": 1.5,
        "miss_sq": 1.2,
    }
    p = true_propensity(x, scenario, coef)
    a = (rng.uniform(size=n) < p).astype(np.int8)

    if scenario == "no_overlap":
        # Deterministic threshold mechanism A = 1[X1 > 0], plus a few units
        # placed DEEP (7 sigma) on the "wrong" side as forced crossovers. The
        # mechanism is structurally positivity-violating (p in {0,1}); the deep
        # crossovers make an out-of-fold *logistic* fit emit scores at the
        # numerical 0/1 boundary rather than merely lacking intersection.
        # Counts/placement are deterministic functions of (n, seed).
        if n < 60:
            raise ValueError("no_overlap scenario requires n >= 60")
        a = (x[:, 0] > 0.0).astype(np.int8)
        n_each = max(5, n // 150)
        order = np.argsort(x[:, 0])
        hi_idx = order[-2 * n_each :][:n_each]
        lo_idx = order[: 2 * n_each][n_each:]
        a[hi_idx] = 0
        a[lo_idx] = 1
        x[hi_idx, 0] = 7.0 + rng.normal(0.0, 0.1, size=n_each)
        x[lo_idx, 0] = -7.0 - rng.normal(0.0, 0.1, size=n_each)
        # Mechanism propensity AFTER relocating the crossover units: treated
        # crossovers sit where p_true=0, untreated crossovers where p_true=1.
        p = (x[:, 0] > 0.0).astype(np.float64)

    tau = tau_const + tau_x * x[:, 0]
    # Baseline outcome shared by both arms.
    mu0 = 0.3 * x[:, 0] - 0.2 * x[:, 1]
    y = mu0 + a * tau + rng.normal(0.0, noise_sd, size=n)
    return SyntheticDataset(
        x=x,
        a=a,
        y=y,
        p_true=p,
        tau=tau,
        ate_true_sample=float(np.mean(tau)),
        scenario=scenario,
        seed=seed,
        coef=coef,
    )
