"""Reproducible synthetic data generation (local fixtures / experiments).

Every dataset is generated from a declared structural model with a seed, so
tests and experiments know the ground truth and the failure being exercised.

Structural form::

    x = W delta + Z Pi + v
    y = W gamma + X beta + e

Endogeneity is induced by ``corr(v, e) = rho``. When ``invalid_instrument`` is
set, one instrument also loads directly on ``e`` (violating the exclusion
restriction) -- used to demonstrate that the over-identification test can
flag trouble even though relevance is perfect.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class DGPSpec:
    n: int = 2000
    n_endogenous: int = 1
    n_exogenous: int = 1
    n_instruments: int = 1
    endogeneity_rho: float = 0.7      # corr(structural error, first-stage error)
    instrument_strength: float = 0.5  # scale of Pi; small => weak instruments
    collinear_instruments: bool = False
    invalid_instrument: bool = False  # last Z enters structural error
    invalid_strength: float = 0.8
    beta: tuple[float, ...] = (1.0,)
    gamma: tuple[float, ...] = ()     # defaults to ones
    seed: int = 12345
    noise_sd: float = 1.0


@dataclass(frozen=True)
class SyntheticDataset:
    columns: dict[str, list[float]]
    true_beta: tuple[float, ...]
    true_gamma: tuple[float, ...]
    spec: DGPSpec


def generate(spec: DGPSpec) -> SyntheticDataset:
    rng = np.random.default_rng(spec.seed)
    n = spec.n
    k, jx, ell = spec.n_endogenous, spec.n_exogenous, spec.n_instruments

    w = rng.normal(size=(n, jx))
    z_base = rng.normal(size=(n, ell))
    if spec.collinear_instruments and ell >= 2:
        # z2..zL are near copies of z1: relevance survives but instruments
        # carry almost no independent information.
        z = z_base.copy()
        for idx in range(1, ell):
            z[:, idx] = z[:, 0] + 1e-4 * rng.normal(size=n)
    else:
        z = z_base

    # Correlated structural/first-stage disturbances (known endogeneity).
    mean = np.zeros(k + 1)
    corr = np.full((k + 1, k + 1), spec.endogeneity_rho)
    np.fill_diagonal(corr, 1.0)
    shocks = rng.multivariate_normal(mean, corr, size=n) * spec.noise_sd
    v = shocks[:, :k]
    e = shocks[:, k]
    if spec.invalid_instrument and ell >= 1:
        e = e + spec.invalid_strength * z[:, -1]

    gamma = spec.gamma or tuple(1.0 for _ in range(jx))
    delta = np.ones((jx, k))
    pi = spec.instrument_strength * np.eye(ell, k) if ell >= k else np.ones((ell, k))

    x = w @ delta + z @ pi + v
    beta = np.asarray(spec.beta if len(spec.beta) == k else tuple(1.0 for _ in range(k)))
    y = (w @ np.asarray(gamma) if jx else np.zeros(n)) + x @ beta + e

    columns: dict[str, list[float]] = {"y": y.tolist()}
    for idx in range(k):
        columns[f"x{idx + 1}"] = x[:, idx].tolist()
    for idx in range(jx):
        columns[f"w{idx + 1}"] = w[:, idx].tolist()
    for idx in range(ell):
        columns[f"z{idx + 1}"] = z[:, idx].tolist()

    return SyntheticDataset(
        columns=columns, true_beta=tuple(float(b) for b in beta),
        true_gamma=tuple(float(g) for g in gamma), spec=spec,
    )


def standard_names(spec: DGPSpec) -> dict[str, list[str]]:
    return {
        "dependent": ["y"],
        "endogenous": [f"x{i + 1}" for i in range(spec.n_endogenous)],
        "exogenous": [f"w{i + 1}" for i in range(spec.n_exogenous)],
        "instruments": [f"z{i + 1}" for i in range(spec.n_instruments)],
    }
