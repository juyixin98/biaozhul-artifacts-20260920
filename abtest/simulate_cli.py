"""Command-line interface: run the standard coverage scenarios.

Usage:
    python -m abtest.simulate_cli [--simulations N] [--confidence LEVEL] [--seed S]
"""

from __future__ import annotations

import argparse
import json

from .simulate import DEFAULT_SEED, run_standard_scenarios


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Estimate Welch-interval coverage on synthetic A/A data."
    )
    parser.add_argument("--simulations", type=int, default=2000)
    parser.add_argument("--confidence", type=float, default=0.95)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()

    results = run_standard_scenarios(
        n_simulations=args.simulations,
        confidence_level=args.confidence,
        seed=args.seed,
    )
    for r in results:
        print(json.dumps(r.to_dict(), indent=2))
    print(
        "\nNominal coverage: "
        f"{args.confidence:.4f}. Empirical coverage within Monte Carlo "
        "error of the nominal value indicates the interval is calibrated "
        "for FIXED-sample use only."
    )


if __name__ == "__main__":
    main()
