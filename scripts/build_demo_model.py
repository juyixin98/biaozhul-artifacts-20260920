"""Build the local synthetic demo model and freeze its quantized artifact.

Everything is synthetic and local: a fixed-seed random float MLP is treated as
the "trained" state, calibrated over a fixed synthetic calibration set, and
frozen to models/. Re-running with the same config produces identical
parameters (calibration is deterministic and bound to the model version).

Usage:
    python scripts/build_demo_model.py [--config configs/demo_model.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from engine.model import (
    TrainedModel,
    calibrate_and_freeze,
    save_artifact,
    save_trained_model,
)


def build(config_path: Path, out_dir: Path) -> None:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    rng = np.random.default_rng(config["seed"])
    architecture = config["architecture"]
    lo, hi = config["input_range"]

    weights = []
    biases = []
    for in_dim, out_dim in zip(architecture[:-1], architecture[1:]):
        weights.append(
            rng.uniform(-1.0, 1.0, size=(out_dim, in_dim))
            * config["weight_scale"]
        )
        biases.append(
            rng.uniform(-1.0, 1.0, size=(out_dim,)) * config["bias_scale"]
        )

    trained = TrainedModel(
        weights=tuple(weights),
        biases=tuple(biases),
        model_id=config["model_id"],
    )
    calibration_inputs = rng.uniform(lo, hi, size=(config["calibration_samples"],
                                                   architecture[0]))

    artifact = calibrate_and_freeze(
        trained,
        calibration_inputs,
        activation_dtype=np.dtype(config["activation_dtype"]),
        weight_dtype=np.dtype(config["weight_dtype"]),
        output_dtype=np.dtype(config["output_dtype"]),
        accumulator_dtype=np.dtype(config["accumulator_dtype"]),
        hidden_relu=config["hidden_relu"],
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = out_dir / f"{trained.model_id}.json"
    trained_path = out_dir / f"{trained.model_id}.trained.json"
    save_artifact(artifact, artifact_path)
    save_trained_model(trained, trained_path)
    print(f"trained state -> {trained_path} (version {trained.version})")
    print(f"frozen artifact -> {artifact_path}")
    print(
        "calibration bound to version "
        f"{artifact.model_version} ({artifact.calibration.sample_count} samples, "
        f"fingerprint {artifact.calibration.calibration_fingerprint})"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("configs/demo_model.json"))
    parser.add_argument("--out-dir", type=Path, default=Path("models"))
    args = parser.parse_args()
    build(args.config, args.out_dir)


if __name__ == "__main__":
    main()
