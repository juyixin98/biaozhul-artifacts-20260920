#!/usr/bin/env python3
"""Reproducible experiment driver.

Regenerates every artifact under ``reports/`` from scratch using only local
synthetic data and fixed seeds. It is deliberately independent of the test
suite: results here are written to disk for human review, never asserted by
the package's own tests (reference answers live in tests/fixtures/).

Outputs:
    reports/good_overlap.json            normal run, known true ATE
    reports/misspecification.json        wrong model -> calibration REJECT
    reports/poor_overlap_clipped.json    fixed clipping disclosed
    reports/no_overlap_error.json        positivity violation (rc envelope)
    reports/coverage_summary.json        Monte Carlo 95% CI coverage vs DGP truth

Usage:
    PYTHONPATH=src python experiments/reproduce.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ipwate.errors import PositivityError
from ipwate.pipeline import run_ipw
from ipwate.synthetic import generate_synthetic

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"


def _write(name: str, payload: dict) -> None:
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / name).write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n")


def scenario_runs() -> None:
    data = generate_synthetic(n=4000, scenario="good_overlap", seed=2024)
    good = run_ipw(data.x, data.a, data.y, request_id="repro-good").to_dict()
    good["ground_truth"] = {
        "ate_true_on_sample": data.ate_true_sample,
        "known_from_dgp": "tau(X)=2+0.5*X1 with X1~N(0,1); E[tau]=2.0",
    }
    _write("good_overlap.json", good)

    data = generate_synthetic(n=6000, scenario="misspecification", seed=33)
    _write(
        "misspecification.json",
        run_ipw(data.x, data.a, data.y, request_id="repro-misspec").to_dict(),
    )

    data = generate_synthetic(n=3000, scenario="poor_overlap", seed=11)
    _write(
        "poor_overlap_clipped.json",
        run_ipw(
            data.x, data.a, data.y,
            overrides={"weights.clipping.enabled": True},
            request_id="repro-poor-clipped",
        ).to_dict(),
    )

    data = generate_synthetic(n=2000, scenario="no_overlap", seed=9)
    try:
        run_ipw(data.x, data.a, data.y, request_id="repro-nooverlap")
        envelope = {"ok": False, "unexpected": "positivity violation did not fire"}
    except PositivityError as exc:
        envelope = {"ok": False, "scenario": "no_overlap", "error": exc.to_dict()}
    _write("no_overlap_error.json", envelope)


def coverage_study(reps: int = 100, n: int = 3000) -> None:
    """Monte Carlo coverage of the nominal 95% CI against KNOWN DGP truth."""
    rng_seeds = range(1000, 1000 + reps)
    records = []
    covered = 0
    errors = 0.0
    for seed in rng_seeds:
        data = generate_synthetic(n=n, scenario="good_overlap", seed=seed)
        result = run_ipw(data.x, data.a, data.y)
        truth = data.ate_true_sample
        hit = result.estimate.ci_lower <= truth <= result.estimate.ci_upper
        covered += int(hit)
        errors += result.estimate.point - truth
        records.append(
            {
                "seed": seed,
                "true_ate": truth,
                "point": result.estimate.point,
                "se": result.estimate.se,
                "ci_covered_truth": hit,
            }
        )
    bias = errors / reps
    se_coverage = float(np.sqrt(0.95 * 0.05 / reps))
    _write(
        "coverage_summary.json",
        {
            "reps": reps,
            "n_per_rep": n,
            "nominal_ci": 0.95,
            "empirical_coverage": covered / reps,
            "mc_standard_error_of_coverage": se_coverage,
            "mean_point_minus_truth": bias,
            "interpretation": (
                "Coverage is a property of the interval procedure under the "
                "known DGP; it says nothing about unconfoundedness in real data."
            ),
            "runs": records,
        },
    )


def main() -> None:
    scenario_runs()
    coverage_study()
    print(f"Reproduction artifacts written to {REPORTS}")


if __name__ == "__main__":
    main()
