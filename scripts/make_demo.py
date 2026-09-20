"""Generate small demo datasets into the first whitelisted data directory.

Run:  python -m scripts.make_demo
Creates:
  data/iris_tiny.csv   - 4-feature, 3-class classification (120 rows)
  data/sine_reg.npy    - 3-feature regression (100 rows)
"""
from __future__ import annotations

import csv
import os

import numpy as np


def _iris_like(n_per_class: int = 40, seed: int = 7) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    centers = np.array([
        [5.0, 3.4, 1.5, 0.2],
        [5.9, 2.8, 4.3, 1.3],
        [6.5, 3.0, 5.6, 2.0],
    ])
    xs, ys = [], []
    for cls, center in enumerate(centers):
        x = center[None, :] + rng.normal(scale=0.35, size=(n_per_class, 4))
        xs.append(x)
        ys.append(np.full(n_per_class, cls, dtype=np.int64))
    x = np.vstack(xs).astype(np.float32)
    y = np.concatenate(ys)
    perm = rng.permutation(len(y))
    return x[perm], y[perm]


def _sine(n: int = 100, seed: int = 11) -> np.ndarray:
    rng = np.random.default_rng(seed)
    x1 = rng.uniform(-2.0, 2.0, n)
    x2 = rng.uniform(-1.0, 1.0, n)
    x3 = rng.normal(size=n)
    y = 1.5 * np.sin(1.3 * x1) + 0.7 * x2 - 0.3 * x3 + rng.normal(scale=0.05, size=n)
    return np.stack([x1, x2, x3, y], axis=1).astype(np.float32)


def main(out_dir: str = "data") -> None:
    os.makedirs(out_dir, exist_ok=True)
    x, y = _iris_like()
    csv_path = os.path.join(out_dir, "iris_tiny.csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sepal_len", "sepal_wid", "petal_len", "petal_wid", "label"])
        for row, label in zip(x, y):
            w.writerow([f"{v:.5f}" for v in row] + [int(label)])

    npy_path = os.path.join(out_dir, "sine_reg.npy")
    np.save(npy_path, _sine())
    print(f"wrote {csv_path} ({len(y)} rows)")
    print(f"wrote {npy_path}")


if __name__ == "__main__":
    main()
