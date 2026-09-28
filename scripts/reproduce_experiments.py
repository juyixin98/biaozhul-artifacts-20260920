#!/usr/bin/env python3
"""Reproducible Monte-Carlo experiments over the seeded synthetic DGP.

Reproduces the three headline experiments and prints a table:

1. known endogeneity  -> OLS badly biased, 2SLS unbiased, DWH rejects
2. weak instruments   -> CD below cutoff, 2SLS biased toward OLS, verdict changes
3. collinear tools    -> order condition holds, VIF explodes, warning recorded

Every stream is seeded; rerunning with the same arguments is bit-reproducible.

Usage::

    python scripts/reproduce_experiments.py [--reps 200]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config  # noqa: E402
from app.contracts import EstimateRequest  # noqa: E402
from app.dgp import DGPSpec, generate, standard_names  # noqa: E402
from app.service import run_estimation  # noqa: E402


def _service_run(spec: DGPSpec, config):
    ds = generate(spec)
    nm = standard_names(spec)
    req = EstimateRequest(
        dependent="y", endogenous=nm["endogenous"], exogenous=nm["exogenous"],
        instruments=nm["instruments"], columns=ds.columns,
        assume_exclusion_restriction=True,
        exclusion_rationale="synthetic DGP (instruments valid by construction unless noted)",
    )
    return ds, run_estimation(req, config)


def _ols_x1(ds) -> float:
    y = np.asarray(ds.columns["y"])
    x = np.asarray(ds.columns["x1"])
    w = np.asarray(ds.columns["w1"])
    R = np.column_stack([np.ones(len(y)), w, x])
    return float(np.linalg.solve(R.T @ R, R.T @ y)[-1])


def experiment_endogeneity(config, reps: int) -> None:
    print("\n[1] Known endogeneity (rho=0.8, strong valid instruments)")
    ols_err, iv_err, dwh_rej = [], [], 0
    for i in range(reps):
        spec = DGPSpec(n=4000, n_instruments=2, endogeneity_rho=0.8,
                       instrument_strength=0.8, seed=7000 + i)
        ds, resp = _service_run(spec, config)
        iv = next(c for c in resp.coefficients if c.name == "x1").estimate
        ols_err.append(_ols_x1(ds) - 1.0)
        iv_err.append(iv - 1.0)
        dwh = next(d for d in resp.diagnostics
                   if d.name == "durbin_wu_hausman_endogeneity")
        dwh_rej += dwh.p_value < 0.05
    print(f"    mean OLS bias     : {np.mean(ols_err):+.4f} (should be large)")
    print(f"    mean 2SLS bias    : {np.mean(iv_err):+.4f} (should be ~0)")
    print(f"    DWH rejection rate: {dwh_rej/reps:.2%} (should be ~100%)")


def experiment_weak(config, reps: int) -> None:
    print("\n[2] Weak instruments (tiny Pi)")
    cds, flags, iv_bias = [], 0, []
    for i in range(reps):
        spec = DGPSpec(n=3000, n_instruments=2, endogeneity_rho=0.8,
                       instrument_strength=0.02, seed=7100 + i)
        _, resp = _service_run(spec, config)
        state = resp.decision.key_state
        cds.append(state["cragg_donald_f"])
        flags += resp.decision.failure_category.value == "weak_instruments"
        iv = next(c for c in resp.coefficients if c.name == "x1").estimate
        iv_bias.append(iv - 1.0)
    print(f"    median Cragg-Donald F: {np.median(cds):.2f} (cutoff 19.93)")
    print(f"    flagged inconclusive : {flags/reps:.2%} (should be ~100%)")
    print(f"    mean 2SLS bias       : {np.mean(iv_bias):+.4f} "
          "(weak-IV bias toward OLS)")


def experiment_collinear(config) -> None:
    print("\n[3] Collinear instruments (z2,z3 ~= z1)")
    spec = DGPSpec(n=3000, n_instruments=3, collinear_instruments=True,
                   instrument_strength=0.8, seed=205)
    _, resp = _service_run(spec, config)
    print(f"    verdict   : {resp.decision.verdict.value}")
    print(f"    max VIF   : {resp.decision.key_state['max_instrument_vif']:.1f}")
    print(f"    rank Z    : {resp.decision.key_state['rank_design']} "
          f"(order/rank survive; independent information does not)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reps", type=int, default=200)
    args = parser.parse_args()
    config = load_config()
    print("=" * 78)
    print(f"Reproducible experiments (reps={args.reps}, seeded, no network)")
    print("=" * 78)
    experiment_endogeneity(config, args.reps)
    experiment_weak(config, args.reps)
    experiment_collinear(config)
    print("\nReproduce exactly: rerun this command (RNG seed list is fixed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
