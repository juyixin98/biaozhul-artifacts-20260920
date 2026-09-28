"""Command-line entry: estimate ATE/ATT from a CSV file.

    python scripts/analyze_csv.py tests/fixtures/good_overlap.csv --estimand ATE
    python scripts/analyze_csv.py tests/fixtures/no_overlap.csv
"""

from __future__ import annotations

import argparse
import logging
import sys

from ipw_ate.contract import Estimand, IPWConfig
from ipw_ate.errors import IPWError
from ipw_ate.io_csv import load_csv
from ipw_ate.pipeline import run_ipw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="IPW-ATE from a CSV file")
    parser.add_argument("path", help="CSV with columns t,y,<covariates...>")
    parser.add_argument("--estimand", choices=["ATE", "ATT"], default="ATE")
    parser.add_argument("--splits", type=int, default=5)
    parser.add_argument("--request-id", default="cli")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    t, y, x, names = load_csv(args.path)
    cfg = IPWConfig(
        estimand=Estimand(args.estimand), n_splits=args.splits
    )
    try:
        res = run_ipw(t, y, x, config=cfg, request_id=args.request_id,
                      feature_names=names)
    except IPWError as exc:
        # Classified failure: print the category, exit non-zero. Never a stack
        # trace for a declared, expected failure mode.
        print(f"REJECTED [{exc.code}]: {exc}", file=sys.stderr)
        return 2

    d = res.diagnostic
    print(f"request_id     : {res.request_id}")
    print(f"estimand       : {res.estimand}  (trim {d.trim_version})")
    print(f"verdict        : {d.decision.value} {list(d.reasons)}")
    print(f"estimate       : {res.estimate:+.4f}")
    print(f"std error (IF) : {res.std_error:.4f}")
    print(f"95% CI         : [{res.ci_lower:+.4f}, {res.ci_upper:+.4f}]")
    print(f"ESS treated    : {d.ess_treated:.1f} / {d.n_treated}")
    print(f"ESS control    : {d.ess_control:.1f} / {d.n_control}")
    print(f"score range    : [{d.score_min:.4f}, {d.score_max:.4f}]")
    print(f"support voids  : {d.single_arm_cells}")
    print(f"max balance z  : {d.max_balance_z:.2f} ({d.balance_basis})")
    print("note: causal only under unconfoundedness/positivity/SUTVA.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
