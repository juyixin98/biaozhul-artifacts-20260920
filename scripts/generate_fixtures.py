"""Generate minimal, deterministic CSV fixtures with a KNOWN generative model.

The generator is independent of the estimator: it writes treatment/outcome/
covariates produced by a documented process (true ATE = 2.0). Run:

    python scripts/generate_fixtures.py
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

from ipw_ate.synthetic import (
    make_extreme_weights_data,
    make_no_overlap_data,
    make_overlap_data,
)

FIX_DIR = Path("tests/fixtures")


def _write(path: Path, data) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["t", "y", "x0", "x1"])
        for ti, yi, row in zip(data.treatment, data.outcome, data.covariates):
            w.writerow([int(ti), f"{yi:.6f}", f"{row[0]:.6f}", f"{row[1]:.6f}"])


def main() -> None:
    FIX_DIR.mkdir(parents=True, exist_ok=True)
    # Small-but-real fixture: 120 units, good overlap, seed fixed.
    _write(FIX_DIR / "good_overlap.csv", make_overlap_data(n=120, seed=11))
    _write(FIX_DIR / "no_overlap.csv", make_no_overlap_data(n=120, seed=44))
    _write(FIX_DIR / "extreme_weights.csv",
           make_extreme_weights_data(n=120, seed=22))
    print("wrote fixtures to", FIX_DIR)


if __name__ == "__main__":
    main()
