#!/usr/bin/env python3
"""End-to-end demo: synthetic data -> split -> deviation report.

Run from the repository root:
    .venv/bin/python examples/run_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from group_split import split_groups
from group_split import synthetic

RATIOS = {"train": 0.7, "val": 0.15, "test": 0.15}
SEED = 42


def show(title: str, groups, labels) -> None:
    result = split_groups(groups, labels, RATIOS, seed=SEED)
    report = result.report
    print(f"\n=== {title} ===")
    print(f"samples={report['n_samples']} groups={report['n_groups']} "
          f"within_tolerance={report['within_tolerance']} "
          f"max_deviation={report['max_deviation']:.4f}")
    for name, s in report["splits"].items():
        print(f"  {name:5s} size={s['size']:5d} target={s['target_size']:8.1f} "
              f"class_counts={s['class_counts']}")
    for reason in report["reasons"]:
        print(f"  reason[{reason['code']}]: {reason['detail']}")


def main() -> None:
    show("balanced", *synthetic.make_balanced(seed=SEED))
    show("large group (45% of samples)", *synthetic.make_with_large_group(seed=SEED))
    show("rare class (4 samples in 1 group)", *synthetic.make_with_rare_class(seed=SEED))

    # Reordering invariance check.
    import numpy as np

    groups, labels = synthetic.make_balanced(seed=SEED)
    perm = np.random.default_rng(7).permutation(len(groups))
    a = split_groups(groups, labels, RATIOS, seed=SEED).assignment
    b = split_groups(groups[perm], labels[perm], RATIOS, seed=SEED).assignment
    print(f"\ninput reordering invariance: {'OK' if a == b else 'FAILED'}")


if __name__ == "__main__":
    main()
