"""Reproducible experiment: run every declared scenario and record results.

Outputs are written to ``results/`` as both JSON (machine-auditable) and a
plain-text report. The run is deterministic given the pinned seed/config.

Scenarios: good overlap, support void (no overlap), extreme-but-interior
weights, a misspecified (non-linear outcome) design, and deterministic
separation. Expected verdicts are declared up front and asserted.

    python scripts/run_experiments.py
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

import numpy as np

from ipw_ate.contract import Decision, Estimand, IPWConfig
from ipw_ate.errors import IPWError
from ipw_ate.pipeline import run_ipw
from ipw_ate.synthetic import (
    make_extreme_weights_data,
    make_misspecified_data,
    make_no_overlap_data,
    make_overlap_data,
    make_propensity_misspecified_data,
    make_separated_data,
)

RESULTS_DIR = Path("results")
TRUE_ATE = 2.0


def _scenarios() -> dict:
    return {
        "good_overlap": (
            make_overlap_data(2000, seed=101),
            IPWConfig(n_splits=5, estimand=Estimand.ATE),
            {Decision.ACCEPT},
        ),
        "support_void": (
            make_no_overlap_data(1000, seed=44),
            IPWConfig(n_splits=4, estimand=Estimand.ATE),
            {Decision.REJECT},
        ),
        "extreme_interior_weights": (
            make_extreme_weights_data(1000, seed=22),
            IPWConfig(n_splits=5, estimand=Estimand.ATE),
            {Decision.INCONCLUSIVE, Decision.REJECT},
        ),
        "misspecified_outcome": (
            make_misspecified_data(2000, seed=33),
            IPWConfig(n_splits=5, estimand=Estimand.ATE),
            {Decision.ACCEPT, Decision.INCONCLUSIVE},
        ),
        "misspecified_propensity": (
            make_propensity_misspecified_data(2000, seed=71),
            IPWConfig(n_splits=5, estimand=Estimand.ATE),
            {Decision.REJECT},
        ),
        "deterministic_separation": (
            make_separated_data(800, seed=55),
            IPWConfig(n_splits=5, estimand=Estimand.ATE),
            "raises",
        ),
    }


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    records: dict[str, dict] = {}
    lines: list[str] = [
        "IPW-ATE reproduction report",
        f"true ATE (by construction) = {TRUE_ATE}",
        "=" * 72,
    ]

    for name, (data, cfg, expected) in _scenarios().items():
        rec: dict = {"expected": list(expected) if expected != "raises" else "raises"}
        try:
            res = run_ipw(
                data.treatment, data.outcome, data.covariates,
                config=cfg, request_id=f"exp_{name}",
            )
            d = res.diagnostic
            rec.update({
                "status": "estimated",
                "decision": d.decision.value,
                "estimate": res.estimate,
                "std_error": res.std_error,
                "ci": [res.ci_lower, res.ci_upper],
                "error_vs_true": res.estimate - TRUE_ATE,
                "ess_treated": d.ess_treated,
                "ess_control": d.ess_control,
                "score_min": d.score_min,
                "score_max": d.score_max,
                "single_arm_cells": d.single_arm_cells,
                "max_balance_z": d.max_balance_z,
                "reasons": list(d.reasons),
                "message": d.message,
            })
            verdict_ok = d.decision in expected
            lines.append(
                f"{name:28s} {d.decision.value:12s} tau={res.estimate:+.3f} "
                f"SE={res.std_error:.3f} balz={d.max_balance_z:.2f} "
                f"CI=[{res.ci_lower:+.3f},{res.ci_upper:+.3f}] "
                f"verdict_ok={verdict_ok}"
            )
        except IPWError as exc:
            rec.update({
                "status": "rejected",
                "error_code": exc.code,
                "message": str(exc),
                "diagnostic": (
                    asdict(exc.diagnostic)
                    if getattr(exc, "diagnostic", None) is not None else None
                ),
            })
            verdict_ok = expected == "raises"
            diag = getattr(exc, "diagnostic", None)
            # A REJECT expectation is met by the overlap-gating exception that
            # carries the REJECT diagnostic.
            if not verdict_ok and diag is not None and expected != "raises":
                verdict_ok = diag.decision in expected
                rec["decision"] = diag.decision.value
            lines.append(f"{name:28s} RAISED {exc.code:28s} verdict_ok={verdict_ok}")

        rec["verdict_ok"] = bool(verdict_ok)
        records[name] = rec

    # Convert numpy scalars / tuples for JSON serialization.
    def default(o):
        if isinstance(o, (np.floating, np.integer)):
            return o.item()
        if isinstance(o, (tuple,)):
            return list(o)
        raise TypeError(type(o))

    (RESULTS_DIR / "experiments.json").write_text(
        json.dumps(records, indent=2, default=default)
    )
    (RESULTS_DIR / "experiments.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))

    ok = all(r["verdict_ok"] for r in records.values())
    print("=" * 72)
    print("ALL VERDICTS MATCH DECLARED EXPECTATIONS" if ok
          else "SOME VERDICTS DID NOT MATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
