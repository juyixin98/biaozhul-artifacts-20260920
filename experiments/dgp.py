"""Synthetic data-generating processes for tests and replication experiments.

All DGPs are local and fully deterministic given a seed. They support the
three failure modes the diagnostics must recognize:

* known endogeneity       : structural error correlated with the endogenous
                            regressor through the first-stage disturbance
* weak instruments        : a small first-stage loading ``pi``
* collinear instruments   : a second instrument equal to the first plus noise
                            (rank preserved but near-redundant), plus an exact
                            duplicate helper for rank-failure tests

Canonical single-endog DGP
--------------------------
Z  (n,L) ~ N(0, I_L), optionally made collinear
[v, e] ~ N(0, [[1, cov_ve], [cov_ve, 1]])           <- endogeneity lives here
X_end = Z @ pi + w_x' b_x + v
y     = beta * X_end + w_x' g_x + e

OLS is inconsistent for beta whenever cov_ve != 0; IV requires pi != 0 and
Z independent of e (true by construction here and asserted, not inferred).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class DGPConfig:
    nobs: int = 2_000
    n_instruments: int = 2
    n_controls: int = 1  # exogenous controls besides intercept
    beta: float = 0.75
    gamma: tuple[float, ...] = (0.4,)
    pi: tuple[float, ...] = (0.5, 0.5)  # first-stage loadings; ~0.05 => weak
    endogeneity: float = 0.6           # cov(v, e)
    collinear_second: bool = False
    collinear_noise: float = 0.01
    exact_duplicate_instrument: bool = False
    zero_first_stage: bool = False     # pi all zero -> rank failure
    invalid_instrument_corr: float = 0.0  # last instrument contaminated by e (over-id violation)
    seed: int = 20260928

    def __post_init__(self) -> None:
        if len(self.pi) != self.n_instruments:
            raise ValueError("pi must have one loading per instrument")
        if len(self.gamma) != self.n_controls:
            raise ValueError("gamma must have one coefficient per exogenous control")


@dataclass(frozen=True)
class SyntheticSample:
    columns: dict[str, np.ndarray]
    config: DGPConfig
    truth: dict[str, float]


def generate(config: DGPConfig) -> SyntheticSample:
    rng = np.random.default_rng(config.seed)
    n, L = config.nobs, config.n_instruments

    Z = rng.standard_normal((n, L))
    if config.collinear_second and L >= 2:
        Z[:, 1:] = Z[:, [0]] + config.collinear_noise * rng.standard_normal((n, L - 1))
    if config.exact_duplicate_instrument and L >= 2:
        Z[:, 1] = Z[:, 0]

    Wx = rng.standard_normal((n, config.n_controls)) if config.n_controls else np.empty((n, 0))

    # correlated disturbances (v, e)
    cov_ve = config.endogeneity
    cov = np.array([[1.0, cov_ve], [cov_ve, 1.0]])
    shocks = rng.multivariate_normal([0.0, 0.0], cov, size=n)
    v, e = shocks[:, 0], shocks[:, 1]

    if config.invalid_instrument_corr:
        if L < 2:
            raise ValueError("an over-id violation needs at least 2 instruments")
        # Contaminate the LAST instrument with structural-error variation.
        # It stays relevant (original Z component drives x_end) but violates
        # Cov(Z,e)=0, so the over-id test must reject when L > k.
        Z[:, -1] = Z[:, -1] + config.invalid_instrument_corr * e

    pi = np.zeros(L) if config.zero_first_stage else np.asarray(config.pi, dtype=float)
    x_end = Z @ pi + (Wx @ np.array(config.gamma) if config.n_controls else 0.0) + v
    y = config.beta * x_end + (Wx @ np.array(config.gamma) if config.n_controls else 0.0) + e

    columns: dict[str, np.ndarray] = {"y": y, "x_end": x_end}
    for j in range(L):
        columns[f"z{j + 1}"] = Z[:, j]
    for j in range(config.n_controls):
        columns[f"w{j + 1}"] = Wx[:, j]

    truth = {"beta": config.beta, "cov_ve": cov_ve, "first_stage_pi_norm": float(np.linalg.norm(pi))}
    return SyntheticSample(columns=columns, config=config, truth=truth)


def strong_iv_sample(n: int = 2_000, seed: int = 1) -> SyntheticSample:
    return generate(DGPConfig(nobs=n, pi=(0.6, 0.6), endogeneity=0.6, seed=seed))


def weak_iv_sample(n: int = 2_000, seed: int = 2) -> SyntheticSample:
    # First-stage population F is small: pi ~ 0.04
    return generate(DGPConfig(nobs=n, pi=(0.04, 0.04), endogeneity=0.6, seed=seed))


def collinear_iv_sample(n: int = 2_000, seed: int = 3) -> SyntheticSample:
    return generate(
        DGPConfig(nobs=n, pi=(0.5, 0.0), collinear_second=True, endogeneity=0.5, seed=seed)
    )


def underidentified_sample(n: int = 2_000, seed: int = 4) -> SyntheticSample:
    return generate(DGPConfig(nobs=n, zero_first_stage=True, seed=seed))


def just_identified_sample(n: int = 2_000, seed: int = 5) -> SyntheticSample:
    cfg = DGPConfig(nobs=n, n_instruments=1, pi=(0.6,), endogeneity=0.6, seed=seed)
    return generate(cfg)


def rank_failure_sample(n: int = 1_000, seed: int = 6) -> SyntheticSample:
    """Sample-exact rank failure: Z is orthogonal to [controls, Y] in-sample.

    Population irrelevance (``zero_first_stage``) is asymptotically rank-zero
    but only *weak* in any finite sample; this fixture instead builds the
    exact algebraic failure the rank diagnostic must catch, while keeping Z
    full-rank and non-constant itself.
    """
    base = generate(DGPConfig(nobs=n, seed=seed))
    cols = base.columns
    Zraw = np.column_stack([cols[f"z{j+1}"] for j in range(base.config.n_instruments)])
    Wx = np.column_stack([cols[f"w{j+1}"] for j in range(base.config.n_controls)])
    Xd = np.column_stack([np.ones(n), Wx])
    Yd = cols["x_end"]
    # annihilate Z against [X, Y]: Z now has Z'X = Z'Y = 0 in this sample
    D = np.column_stack([Xd, Yd])
    Zperp = Zraw - D @ np.linalg.lstsq(D, Zraw, rcond=None)[0]
    out = dict(cols)
    for j in range(Zperp.shape[1]):
        out[f"z{j+1}"] = Zperp[:, j]
    return SyntheticSample(columns=out, config=base.config, truth={**base.truth, "sample_rank_failure": True})


def duplicate_instrument_sample(n: int = 2_000, seed: int = 7) -> SyntheticSample:
    """Exact duplicate instrument -> instrument block rank deficient."""
    return generate(DGPConfig(nobs=n, exact_duplicate_instrument=True, seed=seed))


def invalid_instrument_sample(n: int = 4_000, seed: int = 8) -> SyntheticSample:
    """One valid and one (strongly) invalid instrument -> over-id rejection."""
    return generate(
        DGPConfig(nobs=n, pi=(0.6, 0.6), endogeneity=0.6,
                  invalid_instrument_corr=1.5, seed=seed)
    )


def exogenous_treatment_sample(n: int = 2_000, seed: int = 9) -> SyntheticSample:
    """cov(v,e)=0: treatment is exogenous; OLS and IV both consistent."""
    return generate(DGPConfig(nobs=n, pi=(0.6, 0.6), endogeneity=0.0, seed=seed))


@dataclass(frozen=True)
class MultiEndogConfig:
    nobs: int = 3_000
    k_endog: int = 2
    n_instruments: int = 3
    betas: tuple[float, ...] = (0.75, -0.5)
    seed: int = 10
    weak_index: int | None = None  # index of an endogenous regressor given weak instruments


def generate_multi_endog(config: MultiEndogConfig) -> SyntheticSample:
    """k>1 endogenous regressors: y = Y beta + e,  Y = Z Pi + V."""
    if len(config.betas) != config.k_endog:
        raise ValueError("one beta per endogenous regressor")
    rng = np.random.default_rng(config.seed)
    n, k, L = config.nobs, config.k_endog, config.n_instruments
    Z = rng.standard_normal((n, L))
    V = rng.standard_normal((n, k))
    e = 0.6 * V[:, 0] + 0.4 * V[:, 1] + rng.standard_normal(n)  # both endogenous
    Pi = rng.uniform(0.4, 0.7, size=(L, k))
    if config.weak_index is not None:
        # Only a tiny loading for the chosen endogenous column.
        Pi[:, config.weak_index] = 0.03
    Y = Z @ Pi + V
    y = Y @ np.array(config.betas) + e
    columns: dict[str, np.ndarray] = {"y": y}
    for j in range(k):
        columns[f"y_end{j+1}"] = Y[:, j]
    for j in range(L):
        columns[f"z{j+1}"] = Z[:, j]
    columns["const"] = np.ones(n)
    truth = {f"beta{j+1}": b for j, b in enumerate(config.betas)}
    cfg = DGPConfig(nobs=n, n_instruments=L, pi=tuple(Pi[:, 0]), seed=config.seed)
    return SyntheticSample(columns=columns, config=cfg, truth=truth)


def multi_endog_sample(n: int = 3_000, seed: int = 11) -> SyntheticSample:
    return generate_multi_endog(MultiEndogConfig(nobs=n, seed=seed))


def multi_endog_partial_weak_sample(n: int = 3_000, seed: int = 12) -> SyntheticSample:
    """Two endog regressors, instruments strong for one but weak for the other."""
    return generate_multi_endog(MultiEndogConfig(nobs=n, weak_index=1, seed=seed))
