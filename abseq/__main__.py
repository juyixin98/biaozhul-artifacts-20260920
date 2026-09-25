"""Command-line entry points for the simulation harness.

Usage:
    python -m abseq coverage  --n-control 500 --n-treatment 500 --reps 2000
    python -m abseq peeking   --n-per-group 500 --looks 5 --reps 2000
"""

from __future__ import annotations

import argparse
import json
import sys

from .simulation import coverage_simulation, peeking_type1_simulation


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--reps", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="abseq")
    sub = parser.add_subparsers(dest="command", required=True)

    cov = sub.add_parser("coverage", help="estimate interval coverage by simulation")
    cov.add_argument("--n-control", type=int, required=True)
    cov.add_argument("--n-treatment", type=int, required=True)
    cov.add_argument("--effect", type=float, default=0.0)
    cov.add_argument("--confidence", type=float, default=0.95)
    cov.add_argument("--missing-strategy", default="drop",
                     choices=["raise", "drop", "impute_mean"])
    cov.add_argument("--missing-rate", type=float, default=0.0)
    _add_common(cov)

    peek = sub.add_parser("peeking", help="show type-I inflation from peeking")
    peek.add_argument("--n-per-group", type=int, required=True)
    peek.add_argument("--looks", type=int, default=5)
    peek.add_argument("--alpha", type=float, default=0.05)
    _add_common(peek)

    args = parser.parse_args(argv)
    if args.command == "coverage":
        result = coverage_simulation(
            n_control=args.n_control,
            n_treatment=args.n_treatment,
            effect=args.effect,
            reps=args.reps,
            confidence=args.confidence,
            missing_strategy=args.missing_strategy,
            missing_rate=args.missing_rate,
            seed=args.seed,
        )
    else:
        result = peeking_type1_simulation(
            n_per_group=args.n_per_group,
            looks=args.looks,
            reps=args.reps,
            alpha=args.alpha,
            seed=args.seed,
        )
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
