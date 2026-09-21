"""Generate a small demo dataset under the whitelisted data/ directory.

Creates:
  iris_demo.csv       - 150 samples, 4 features + integer class label
  regression_demo.csv - 120 samples, 3 features + continuous target
  npy_demo_X.npy / npy_demo_y.npy - same classification data in npy form
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np


def _data_dir() -> Path:
    whitelist = os.environ.get("DATA_WHITELIST")
    if whitelist:
        # Use the first colon-separated whitelist root.
        return Path(whitelist.split(os.pathsep)[0])
    return Path(__file__).resolve().parent.parent / "data"


DATA = _data_dir()


def make_classification(n: int = 150, seed: int = 7) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    centers = np.array([[2.0, 1.0, 0.0, -1.0],
                        [-1.5, 2.0, 1.5, 0.5],
                        [0.5, -2.0, 2.0, 1.0]], dtype=np.float32)
    labels = rng.integers(0, 3, size=n)
    X = centers[labels] + rng.normal(scale=0.7, size=(n, 4)).astype(np.float32)
    return X.astype(np.float32), labels.astype(np.int64)


def make_regression(n: int = 120, seed: int = 11) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, 3)).astype(np.float32)
    w = np.array([1.5, -2.0, 0.7], dtype=np.float32)
    y = X @ w + 0.3 * rng.normal(size=n).astype(np.float32) + 0.5
    return X, y.astype(np.float32)


def _write_csv(path: Path, X: np.ndarray, y: np.ndarray) -> None:
    with open(path, "w") as fh:
        for row, target in zip(X, y):
            cols = ", ".join(f"{v:.6f}" for v in row)
            if y.dtype.kind == "i":
                fh.write(f"{cols}, {int(target)}\n")
            else:
                fh.write(f"{cols}, {float(target):.6f}\n")


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    Xc, yc = make_classification()
    _write_csv(DATA / "iris_demo.csv", Xc, yc)
    np.save(DATA / "npy_demo_X.npy", Xc)
    np.save(DATA / "npy_demo_y.npy", yc)

    Xr, yr = make_regression()
    _write_csv(DATA / "regression_demo.csv", Xr, yr)
    print(f"wrote demo data to {DATA}")
    for p in sorted(DATA.iterdir()):
        print(" -", p.name, p.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
