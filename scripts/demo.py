#!/usr/bin/env python3
"""Local end-to-end demo: exercise the service with synthetic fixtures.

Runs four scenarios with known ground truth and prints, per scenario, only
non-sensitive metadata plus the decision record: request id, payload
fingerprint, verdict, failure category, key state and reasons. Raw
observations are never printed.

Usage::

    python scripts/demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config  # noqa: E402
from app.contracts import EstimateRequest  # noqa: E402
from app.dgp import DGPSpec, generate, standard_names  # noqa: E402
from app.errors import TwoSLSError  # noqa: E402
from app.logging_context import bind_request_id, redacted_summary  # noqa: E402
from app.service import run_estimation  # noqa: E402

SCENARIOS = [
    ("strong valid instruments",
     DGPSpec(n=6000, n_instruments=2, instrument_strength=0.8, seed=407)),
    ("weak instruments",
     DGPSpec(n=3000, n_instruments=2, instrument_strength=0.02, seed=402)),
    ("collinear instruments",
     DGPSpec(n=3000, n_instruments=3, collinear_instruments=True,
             instrument_strength=0.8, seed=205)),
    ("invalid instrument (exclusion violated)",
     DGPSpec(n=6000, n_instruments=3, invalid_instrument=True,
             invalid_strength=1.2, instrument_strength=0.7, seed=206)),
]


def _print_scenario(title: str, spec: DGPSpec, config) -> None:
    ds = generate(spec)
    names = standard_names(spec)
    rid = bind_request_id()
    req = EstimateRequest(
        dependent="y", endogenous=names["endogenous"],
        exogenous=names["exogenous"], instruments=names["instruments"],
        columns=ds.columns, assume_exclusion_restriction=True,
        exclusion_rationale=(
            "declared for demo; note the last scenario violates it by design"
        ),
        request_id=rid,
    )
    print(f"\n### scenario: {title}")
    print(f"  payload (redacted): {redacted_summary(ds.columns)}")
    try:
        resp = run_estimation(req, config)
    except TwoSLSError as exc:
        print(f"  REQUEST FAILED  code={exc.code}")
        print(f"  message: {exc.message}")
        print(f"  details: {exc.details}")
        return
    _print_verdict(resp)


def _print_verdict(resp) -> None:
    d = resp.decision
    print(f"  request_id : {d.request_id}")
    print(f"  fingerprint: {d.fingerprint}  n={d.n_obs}  "
          f"K={resp.n_endogenous} L={resp.n_instruments}")
    print(f"  status     : {resp.status}")
    print(f"  verdict    : {d.verdict.value}  "
          f"failure_category={d.failure_category.value}")
    print("  coefficients:")
    for c in resp.coefficients:
        print(f"    {c.name:>8s} = {c.estimate:+.4f}  (se {c.std_error:.4f}, "
              f"p {c.p_value:.4g})")
    print("  key state  :")
    for k, v in d.key_state.items():
        print(f"    {k}: {v}")
    print("  reasons    :")
    for r in d.reasons:
        print(f"    - {r}")


def main() -> int:
    config = load_config()
    print("=" * 78)
    print("2SLS service - local synthetic demo")
    print("=" * 78)
    for title, spec in SCENARIOS:
        _print_scenario(title, spec, config)
    print("\nNote: structural true beta is 1.0 for x1 in every scenario.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
