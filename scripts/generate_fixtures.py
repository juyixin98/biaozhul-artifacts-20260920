#!/usr/bin/env python3
"""Generate deterministic local synthetic fixtures.

Run once (or after changing the model version); produces:

    fixtures/<model_id>.weights.npz   float fc1/fc2 weights + biases
    fixtures/<model_id>.calib.npz    fixed calibration batch
    fixtures/<model_id>.meta.json     identity metadata

No network, no randomness — a fixed seed makes every artifact reproducible.
"""

from __future__ import annotations

import argparse
import json
import os

import numpy as np


def generate(fixture_dir: str, model_id: str, model_version: str) -> None:
    rng = np.random.default_rng(20260928)
    os.makedirs(fixture_dir, exist_ok=True)

    # Tiny two-layer MLP: 4 -> 3 -> 2. Small enough to hand-check, with
    # deliberately different per-channel weight scales.
    fc1 = rng.normal(0.0, 0.5, size=(3, 4)).astype(np.float32)
    fc2 = rng.normal(0.0, 0.3, size=(2, 3)).astype(np.float32)
    fc1_bias = np.array([0.25, -0.5, 0.1], dtype=np.float32)
    fc2_bias = np.array([-0.2, 0.4], dtype=np.float32)

    # Calibration batch: include negatives, positives and near-zero values so
    # the affine zero point is non-zero and away from either code bound.
    x = rng.normal(0.0, 1.0, size=(64, 4)).astype(np.float32)

    np.savez(
        os.path.join(fixture_dir, f"{model_id}.weights.npz"),
        fc1=fc1, fc2=fc2, fc1_bias=fc1_bias, fc2_bias=fc2_bias,
    )
    np.savez(os.path.join(fixture_dir, f"{model_id}.calib.npz"), x=x)
    with open(os.path.join(fixture_dir, f"{model_id}.meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"model_id": model_id, "model_version": model_version,
                   "input_dim": 4, "hidden": 3, "output": 2}, fh, indent=2)
    print(f"fixtures written to {fixture_dir} for {model_id}@{model_version}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-dir", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures"))
    parser.add_argument("--model-id", default="tiny-matmul-demo")
    parser.add_argument("--model-version", default="2026-09-28-v1")
    args = parser.parse_args()
    generate(args.fixture_dir, args.model_id, args.model_version)


if __name__ == "__main__":
    main()
