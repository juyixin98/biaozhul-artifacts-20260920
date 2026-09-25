#!/usr/bin/env python3
"""Library-API usage example (no CLI, no files required).

Run from the repository root:

    python3 examples/python_api_example.py

Demonstrates:
  * fitting on TRAIN data only,
  * transforming TEST rows whose columns are supplied in a different order,
  * an unseen category (unknown-city indicator column),
  * a zero-variance numeric column (safe -> all zeros),
  * saving to / loading from JSON and getting identical inference.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Allow running directly (`python3 examples/python_api_example.py`) without install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from feature_pipeline import (
    ColumnSpec,
    FeaturePipeline,
    load_pipeline,
    save_pipeline,
)


def main() -> None:
    specs = [
        ColumnSpec(name="age", dtype="numeric",
                   impute_strategy="mean", scale=True),
        ColumnSpec(name="income", dtype="numeric",
                   impute_strategy="constant", fill_value=0.0, scale=True),
        ColumnSpec(name="city", dtype="categorical",
                   impute_strategy="most_frequent", handle_unknown="indicator"),
        ColumnSpec(name="plan_tier", dtype="numeric",
                   impute_strategy="constant", fill_value=7.0, scale=True),
    ]

    # ---- TRAIN: the only data the fit ever sees --------------------------- #
    train = {
        "age": np.array([22.0, 38.0, np.nan, 56.0, 29.0, 41.0]),
        "income": np.array([4200.0, 7800.0, 5100.0, np.nan, 6300.0, 9100.0]),
        "city": np.array(["Beijing", "Shanghai", "Beijing",
                          "Shenzhen", None, "Beijing"], dtype=object),
        "plan_tier": np.array([7.0, 7.0, 7.0, 7.0, 7.0, 7.0]),
    }
    pipeline = FeaturePipeline(specs).fit(train)
    print("Fitted output feature order:")
    print(" ", pipeline.feature_names_out)

    # ---- TEST: shuffled columns, unseen city "Hangzhou", nulls, const col -- #
    test_shuffled = {
        "plan_tier": np.array([7.0, 7.0, 7.0]),
        "city": np.array(["Hangzhou", None, "Shenzhen"], dtype=object),
        "income": np.array([np.nan, 5500.0, 12000.0]),
        "age": np.array([31.0, np.nan, 80.0]),
    }
    result = pipeline.transform(test_shuffled)
    print("\nTransformed matrix:")
    with np.printoptions(precision=4, suppress=True):
        print(result.X)
    print("\nUnknown-city rows:",
          result.unknown_categories["city"].tolist())

    # ---- save / load parity ------------------------------------------------ #
    with tempfile.TemporaryDirectory() as tmp:
        artifact = Path(tmp) / "pipeline.json"
        save_pipeline(pipeline, artifact)
        restored = load_pipeline(artifact)
        again = restored.transform(test_shuffled)
        assert np.array_equal(again.X, result.X)
        assert restored.feature_names_out == pipeline.feature_names_out
        print(f"\nSaved/loaded {artifact.name}; inference is identical. OK")


if __name__ == "__main__":
    main()
