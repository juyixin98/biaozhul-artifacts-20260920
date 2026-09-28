"""Generate the deterministic local sample dataset used by examples.

Run:  python examples/generate_sample_data.py
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from adam_shards.graph import synthetic_dataset  # noqa: E402


def main() -> None:
    out = os.path.join(os.path.dirname(__file__), "sample_dataset.npz")
    x, y = synthetic_dataset(n_samples=40, in_dim=3, n_classes=2, seed=20260927)
    np.savez(out, x=x, y=y)
    print(f"wrote {out}: x={x.shape} y={y.shape} classes={sorted(set(y.tolist()))}")


if __name__ == "__main__":
    main()
