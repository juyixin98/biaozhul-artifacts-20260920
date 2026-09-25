#!/usr/bin/env python3
"""Run the offline grouped-split demo and print a JSON report.

Usage:
    python scripts/demo.py                 # defaults: 2000 samples, seed 42
    python scripts/demo.py --samples 500 --groups 20 --seed 7
    python scripts/demo.py --no-model      # split + checks only
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grouped_splitter.pipeline import run_synthetic_demo  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=2000)
    parser.add_argument("--features", type=int, default=8)
    parser.add_argument("--classes", type=int, default=3)
    parser.add_argument("--groups", type=int, default=40)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--tolerance", type=float, default=0.05)
    parser.add_argument("--no-model", action="store_true")
    parser.add_argument("--output", type=Path, default=None,
                        help="optional path to also write the JSON report")
    args = parser.parse_args(argv)

    report = run_synthetic_demo(
        n_samples=args.samples,
        n_features=args.features,
        n_classes=args.classes,
        n_groups=args.groups,
        seed=args.seed,
        tolerance=args.tolerance,
        train_model=not args.no_model,
    )
    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
