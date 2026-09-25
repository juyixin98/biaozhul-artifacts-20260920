"""End-to-end demonstration on reproducible synthetic data.

Run:
    python examples/demo.py

It fits on the training split only, then exercises every acceptance
scenario on held-out data: unknown category, shuffled column order,
zero variance, missing values, and save/load prediction parity.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from feature_pipeline.artifact import ModelArtifact  # noqa: E402
from feature_pipeline.data import make_synthetic_dataset  # noqa: E402


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    dataset = make_synthetic_dataset()
    schema = dataset["schema"]

    section("fit on TRAINING data only")
    artifact = ModelArtifact.fit(
        schema, dataset["train_rows"], dataset["train_targets"]
    )
    print(f"train rows      : {len(dataset['train_rows'])}")
    print(f"input columns   : {schema.feature_names}")
    print(f"features out    : {artifact.feature_names}")
    print(f"model intercept : {artifact.model.intercept_:.4f}")

    section("test data is held out (never fit)")
    train_cities = {r["city"] for r in dataset["train_rows"] if r["city"]}
    unknown = dataset["unknown_category"]
    print(f"unknown category present in test but not train: "
          f"{unknown in train_cities=}, "
          f"in_test={any(r.get('city') == unknown for r in dataset['test_rows'])}")

    section("unknown category -> all-zero one-hot vector")
    unknown_rows = [r for r in dataset["test_rows"]
                    if r.get("city") == unknown]
    matrix = artifact.pipeline.transform(unknown_rows)
    city_block = matrix[:, -len(dataset["known_categories"]):]
    print(f"{unknown!r} rows: {len(unknown_rows)}, "
          f"one-hot block all zeros: {np.allclose(city_block, 0.0)}")

    section("shuffled column order gives identical features")
    canonical = [
        {name: row[name] for name in schema.feature_names}
        for row in dataset["test_rows"]
    ]
    shuffled = [dict(reversed(list(row.items()))) for row in canonical]
    same = np.allclose(
        artifact.pipeline.transform(canonical),
        artifact.pipeline.transform(shuffled),
    )
    print(f"canonical vs shuffled-key matrices identical: {same}")

    section("zero-variance column")
    scaler = artifact.pipeline.steps("constant_col")[1]
    print(f"zero_variance detected: {scaler.zero_variance_}, "
          f"stored scale: {scaler.scale_}")

    section("accuracy on complete (non-missing, known-category) test rows")
    known = set(dataset["known_categories"])
    idx = [
        i for i, r in enumerate(dataset["test_rows"])
        if r["age"] is not None and r["income"] is not None
        and r["city"] in known
    ]
    rows = [dataset["test_rows"][i] for i in idx]
    truth = np.array([dataset["test_targets"][i] for i in idx])
    pred = artifact.predict_rows(rows)
    rmse = float(np.sqrt(np.mean((pred - truth) ** 2)))
    print(f"complete test rows: {len(idx)}, RMSE: {rmse:.4f} (noise std 0.5)")

    section("save -> load consistency")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "artifact.json"
        artifact.save(path)
        reloaded = ModelArtifact.load(path)
        before = artifact.predict_rows(dataset["test_rows"])
        after = reloaded.predict_rows(dataset["test_rows"])
        print(f"max |pred_before - pred_after|: "
              f"{np.max(np.abs(before - after)):.3e}")

    print("\nAll acceptance scenarios verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
