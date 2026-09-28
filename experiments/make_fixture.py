#!/usr/bin/env python3
"""Regenerate the checked-in CSV fixture deterministically.

Run from the repository root:
    PYTHONPATH=src python experiments/make_fixture.py

The resulting CSV and its metadata JSON are committed so tests and reviewers
use identical bytes. Do NOT regenerate casually: the reference ATE in the
metadata is used by tests/test_csv_fixture.py.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from ipwate.synthetic import generate_synthetic

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures"


def main() -> None:
    data = generate_synthetic(n=300, scenario="good_overlap", seed=2024)
    csv_path = FIXTURE / "synth_good_n300.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["x0", "x1", "x2", "a", "y"])
        for i in range(len(data.a)):
            writer.writerow(
                [
                    f"{data.x[i, 0]:.10f}",
                    f"{data.x[i, 1]:.10f}",
                    f"{data.x[i, 2]:.10f}",
                    int(data.a[i]),
                    f"{data.y[i]:.10f}",
                ]
            )
    meta = {
        "description": (
            "300-unit good-overlap synthetic sample, seed 2024 (see ipwate.synthetic). "
            "Independent reference: true ATE = mean of tau(X) = 2 + 0.5*x0."
        ),
        "n": 300,
        "scenario": "good_overlap",
        "seed": 2024,
        "ate_true_on_sample": data.ate_true_sample,
        "n_treated": int(data.a.sum()),
    }
    (FIXTURE / "synth_good_n300.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"Wrote {csv_path} (true ATE={data.ate_true_sample:.6f})")


if __name__ == "__main__":
    main()
