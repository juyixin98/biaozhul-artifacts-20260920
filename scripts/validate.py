#!/usr/bin/env python3
"""Validation harness: run interval analysis and exhaustive differential
checks over every example program, then print a report showing:

  * abstract alarms (possible / certain),
  * concrete crash sites found by exhaustive bounded execution,
  * MISSED alarms (would violate soundness -- expected none),
  * SPURIOUS alarms: analyzer warns but no bounded input crashes there
    (the documented conservativity boundary).

Usage:  python scripts/validate.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from interval_ai.diffcheck import exhaustive_check, exhaustive_value_check
from interval_ai.pipeline import analyze_source


# Bounded input region for each example (input var name -> (lo, hi)).
# Arrays are fixed (zero-initialized); the region is chosen small enough to
# enumerate but to straddle the guard/boundary on both sides.
CASES = [
    ("examples/programs/safe_loop.ivl", {}),
    ("examples/programs/correlation_loss.ivl", {"x": (-15, 15)}),
    ("examples/programs/negative_index.ivl", {}),
    ("examples/programs/guarded_index.ivl", {"k": (-20, 20)}),
    # Region that DOES trigger the out-of-bounds write (n >= 5):
    ("examples/programs/unknown_loop.ivl", {"n": (-3, 8)}),
    ("examples/programs/modulo.ivl", {"a": (-8, 20), "m": (-3, 8)}),
    ("examples/programs/mixed.ivl", {"k": (-20, 20)}),
    # Programs whose abstract alarm is spurious over the whole region:
    ("examples/programs/spurious_correlation.ivl", {"x": (-30, 30)}),
    ("examples/programs/spurious_array.ivl", {}),
    # Same loop, region restricted to safe inputs: still one possible alarm,
    # zero concrete crashes -- the conservativity boundary under bounds the
    # analysis cannot recover.
    ("examples/programs/unknown_loop.ivl", {"n": (0, 4)}),
]


def read(path: str) -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, path), "r", encoding="utf-8") as f:
        return f.read()


def main() -> int:
    failures = 0
    for path, ranges in CASES:
        src = read(path)
        name = os.path.basename(path)
        print("=" * 72)
        print(f"PROGRAM: {name}   input region: {ranges or '(no scalar inputs)'}")
        print("-" * 72)

        res = analyze_source(src)
        abstract = sorted(
            (a.kind, a.certainty) for a in res.result.alarms)
        print(f"abstract alarms : {abstract}")

        rep = exhaustive_check(src, ranges)
        concrete = {k: len(v) for k, v in rep.crashes.items()}
        print(f"points checked  : {rep.points_checked}")
        print(f"concrete crashes: {concrete}  (site -> #inputs crashing)")
        print(f"MISSED (unsound): {rep.missed}")
        print(f"spurious/conservative alarms: {rep.spurious}")

        vrep = exhaustive_value_check(src, ranges)
        print(f"exit-value invariant violations: {len(vrep.violations)}")

        ok = rep.sound and not vrep.violations
        print(f"RESULT: {'SOUND' if ok else '*** UNSOUND ***'}")
        if not ok:
            failures += 1

    print("=" * 72)
    if failures:
        print(f"{failures} program(s) FAILED the soundness contract")
        return 1
    print("All programs satisfy the soundness contract over the bounded "
          "input regions.")
    print("(Spurious alarms above are the precision boundary of the "
          "non-relational interval domain.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
