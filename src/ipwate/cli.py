#!/usr/bin/env python3
"""Minimal command-line entry point for local reproducible runs.

Examples:
    python -m ipwate.cli --scenario good_overlap --n 2000 --seed 42
    python -m ipwate.cli --scenario no_overlap --n 1500
    python -m ipwate.cli --scenario poor_overlap --clipping --estimand ate
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import load_config
from .errors import IPWError
from .pipeline import run_ipw
from .synthetic import generate_synthetic


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="IPW-ATE synthetic runner")
    parser.add_argument(
        "--scenario",
        choices=["good_overlap", "poor_overlap", "no_overlap", "misspecification"],
        default="good_overlap",
    )
    parser.add_argument("--n", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--estimand", choices=["ate", "att", "atu"], default="ate")
    parser.add_argument("--weight-type", choices=["stabilized", "ht"], default="stabilized")
    parser.add_argument("--n-splits", type=int, default=5)
    parser.add_argument("--clipping", action="store_true", help="enable FIXED clipping profile")
    parser.add_argument("--request-id", default=None)
    parser.add_argument("--persist", action="store_true", help="store evidence record in SQLite")
    parser.add_argument("--show-true-ate", action="store_true")
    args = parser.parse_args(argv)

    config = load_config()
    data = generate_synthetic(
        n=args.n, scenario=args.scenario, seed=args.seed
    )
    overrides = {
        "estimand": args.estimand,
        "weight_type": args.weight_type,
        "crossfit.n_splits": args.n_splits,
    }
    if args.clipping:
        overrides["weights.clipping.enabled"] = True

    try:
        result = run_ipw(
            data.x,
            data.a,
            data.y,
            config=config,
            overrides=overrides,
            request_id=args.request_id,
            persist=args.persist,
        )
    except IPWError as exc:
        json.dump(
            {"ok": False, "scenario": args.scenario, "error": exc.to_dict()},
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 2

    payload = result.to_dict()
    if args.show_true_ate:
        payload["ground_truth"] = {
            "ate_true_on_sample": data.ate_true_sample,
            "known_from_dgp": "E[tau(X)] = tau_const + tau_x*E[X1] = tau_const",
        }
    json.dump(payload, sys.stdout, indent=2, allow_nan=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
