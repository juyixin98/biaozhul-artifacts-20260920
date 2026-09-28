#!/usr/bin/env python3
"""Local end-to-end demonstration of the 2SLS service (no HTTP needed).

Walks every decision class the service can produce on synthetic data:

  1. strong, valid instruments      -> status=ok
  2. weak instruments               -> status=weak (estimate + warning)
  3. weak instruments, strict=true  -> rejected WEAK_INSTRUMENTS
  4. sample-exact rank failure      -> rejected NOT_IDENTIFIED
  5. contaminated instrument        -> status=inconclusive (over-id reject)
  6. evidence persistence           -> SQLite record of the verdicts

Only aggregate statistics are printed; raw observations never leave the core.
Run:
    PYTHONPATH=src python3 scripts/demo_local.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

logging.disable(logging.CRITICAL)

import numpy as np  # noqa: E402

from experiments.dgp import (  # noqa: E402
    invalid_instrument_sample,
    rank_failure_sample,
    strong_iv_sample,
    weak_iv_sample,
)
from twosls.contract import (  # noqa: E402
    EstimationOptions,
    EstimationRequest,
    InstrumentValidityClaim,
    ModelSpec,
)
from twosls.errors import TwoslsError  # noqa: E402
from twosls.estimator import estimate  # noqa: E402
from twosls.evidence import EvidenceStore  # noqa: E402

TRUTH_BETA = 0.75


def to_columns(sample) -> tuple[dict[str, list[float]], ModelSpec]:
    cols = {k: np.asarray(v, dtype=float).tolist() for k, v in sample.columns.items()}
    n = len(cols["y"])
    cols["const"] = [1.0] * n
    spec = ModelSpec(
        dependent="y",
        endogenous=["x_end"],
        included_exogenous=["w1", "const"],
        excluded_instruments=sorted(k for k in cols if k.startswith("z")),
    )
    return cols, spec


def make_request(sample, request_id: str, strict: bool = False) -> EstimationRequest:
    cols, spec = to_columns(sample)
    return EstimationRequest(
        request_id=request_id,
        columns=cols,
        spec=spec,
        options=EstimationOptions(strict=strict),
        validity_claim=InstrumentValidityClaim(
            exclusion_restriction_asserted=True,
            rationale="demo DGP: instruments generated independently of e",
        ),
    )


def banner(title: str) -> None:
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def show_outcome(out) -> None:
    print(f"request_id : {out.request_id}")
    print(f"status     : {out.status.upper()}")
    print(f"nobs       : {out.nobs} (k={out.n_endog}, m={out.n_exog}, L={out.n_instruments})")
    print("coefficients:")
    for c in out.coefficients:
        print(f"  {c.name:8s} beta={c.estimate:+.4f}  se={c.std_error:.4f}  "
              f"95% CI [{c.ci_low:+.4f}, {c.ci_high:+.4f}]")
    print(f"first-stage F            : {out.first_stage[0].f_statistic:.3f}")
    print(f"first-stage partial R2   : {out.first_stage[0].partial_r2:.4f}")
    print(f"Cragg-Donald statistic   : {out.identification.cragg_donald_statistic:.3f}")
    ov = out.overidentification
    print(f"over-id ({ov.test_name:9s}): "
          f"{ov.verdict}" + (f" (stat={ov.statistic:.3f}, p={ov.p_value:.4g})" if ov.testable else ""))
    print(f"endogeneity (DWH)        : {out.endogeneity.verdict} (p={out.endogeneity.p_value:.3g})")
    print("exclusion assumption     : "
          f"asserted_by_caller={out.assumptions['exclusion_restriction']['asserted_by_caller']}, "
          "derivable_from_data=False")
    if out.warnings:
        print("warnings:")
        for w in out.warnings:
            print(f"  - {w}")


def main() -> int:
    db_path = Path(tempfile.mkdtemp(prefix="twosls-demo-")) / "evidence.db"
    store = EvidenceStore(db_path)

    banner("1. STRONG, VALID INSTRUMENTS  (expect status=ok, beta near truth 0.75)")
    out = estimate(make_request(strong_iv_sample(), "demo-strong"))
    show_outcome(out)
    store.record_outcome(out)
    assert abs(out.coefficient_map()["x_end"].estimate - TRUTH_BETA) < 0.1

    banner("2. WEAK INSTRUMENTS, NON-STRICT  (expect status=weak with a returned estimate)")
    out = estimate(make_request(weak_iv_sample(), "demo-weak"))
    show_outcome(out)
    store.record_outcome(out)

    banner("3. WEAK INSTRUMENTS, strict=true  (expect rejection WEAK_INSTRUMENTS)")
    try:
        estimate(make_request(weak_iv_sample(), "demo-weak-strict", strict=True))
    except TwoslsError as exc:
        print(f"rejected: code={exc.code} request_id={exc.request_id}")
        print("key_state:", json.dumps(exc.key_state, indent=2))
        store.record_error(exc.request_id, exc.code, exc.key_state)

    banner("4. SAMPLE-EXACT RANK FAILURE  (expect rejection NOT_IDENTIFIED)")
    try:
        estimate(make_request(rank_failure_sample(), "demo-rank"))
    except TwoslsError as exc:
        print(f"rejected: code={exc.code} request_id={exc.request_id}")
        for reason in exc.key_state.get("reasons", []):
            print(f"  reason: {reason}")
        store.record_error(exc.request_id, exc.code, exc.key_state)

    banner("5. CONTAMINATED INSTRUMENT  (expect status=inconclusive, over-id reject)")
    out = estimate(make_request(invalid_instrument_sample(), "demo-invalid"))
    show_outcome(out)
    store.record_outcome(out)

    banner("6. EVIDENCE RECORDS IN SQLITE (verdicts + key state only, no raw data)")
    for row in store.list_runs():
        print(f"  {row['request_id']:20s} status={row['status']:12s} "
              f"error_code={row['error_code']} nobs={row['nobs']}")
    one = store.get_run("demo-rank")
    print("\nredacted detail for the NOT_IDENTIFIED run:")
    print(" ", json.dumps(one["key_state"], indent=2))

    print(f"\nevidence database: {db_path}")
    print("demo finished: all decision classes exercised.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
