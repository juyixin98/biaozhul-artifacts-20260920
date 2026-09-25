"""Reproducible synthetic-data demonstration. No external data or models.

Runs the full acceptance scenario with a fixed random seed:
    1. Generate a deterministic train/test split (test rows never touch fit).
    2. Fit the pipeline on TRAIN only.
    3. Transform TEST with: shuffled column order, an unseen category,
       a zero-variance column, and missing values.
    4. Save -> load and prove byte-level parity of inference.
    5. Emit example request/response files into the output directory.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .pipeline import ColumnSpec, FeaturePipeline
from .serialization import load_pipeline, save_pipeline

SEED = 20260925
CITIES = ["Beijing", "Shanghai", "Shenzhen"]
LEVELS = ["bronze", "silver", "gold"]


def _synthetic_data(rng: np.random.Generator, n_rows: int,
                    include_unknown_city: bool = False) -> dict[str, np.ndarray]:
    age = rng.normal(loc=35.0, scale=12.0, size=n_rows).round(1)
    income = (rng.lognormal(mean=10.0, sigma=0.5, size=n_rows)).round(2)
    score = rng.uniform(0.0, 100.0, size=n_rows).round(1)
    city = rng.choice(CITIES, size=n_rows, p=[0.5, 0.3, 0.2])
    level = rng.choice(LEVELS, size=n_rows)

    if include_unknown_city:
        # A category absent from training: first test row only.
        city = city.copy()
        city[0] = "Hangzhou"

    data = {
        "age": age,
        "income": income,
        "score": score,
        "city": city.astype(object),
        "level": level.astype(object),
        "plan_tier": np.full(n_rows, 7.0),  # numeric constant -> zero variance
    }

    # Inject missing values deterministically (not on row 0 of unknown-city data).
    def knock_out(arr: np.ndarray, positions: list[int]) -> np.ndarray:
        out = np.array(arr, dtype=object if arr.dtype.kind in "USO" else float)
        for pos in positions:
            out[pos] = None if out.dtype == object else np.nan
        return out

    data["age"] = knock_out(data["age"], [1, n_rows - 1])
    data["income"] = knock_out(data["income"], [2])
    data["city"] = knock_out(data["city"].astype(object), [3])
    return data


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )


def _to_jsonable_data(data: dict[str, np.ndarray]) -> dict[str, list]:
    out: dict[str, list] = {}
    for name, col in data.items():
        out[name] = [None if (v is None or (isinstance(v, float) and np.isnan(v)))
                     else (float(v) if not isinstance(v, str) else v) for v in col]
    return out


def run_demo(outdir: Path) -> "TransformResult":
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(SEED)
    train = _synthetic_data(rng, n_rows=200)
    test_rng = np.random.default_rng(SEED + 1)
    test = _synthetic_data(test_rng, n_rows=6, include_unknown_city=True)

    specs = [
        ColumnSpec(name="age", dtype="numeric", impute_strategy="mean", scale=True),
        ColumnSpec(name="income", dtype="numeric", impute_strategy="median",
                   scale=True),
        ColumnSpec(name="score", dtype="numeric", impute_strategy="constant",
                   fill_value=0.0, scale=True),
        ColumnSpec(name="city", dtype="categorical",
                   impute_strategy="most_frequent", handle_unknown="indicator"),
        ColumnSpec(name="level", dtype="categorical",
                   impute_strategy="constant", fill_value="unknown_level",
                   handle_unknown="ignore"),
        ColumnSpec(name="plan_tier", dtype="numeric", impute_strategy="constant",
                   fill_value=7.0, scale=True),  # zero-variance guard
    ]

    print("=" * 72)
    print("FITTING on 200 deterministic TRAIN rows (test data never seen)")
    print("=" * 72)
    pipeline = FeaturePipeline(specs).fit(train)
    print("Output feature order:")
    for i, name in enumerate(pipeline.feature_names_out):
        print(f"  [{i:2d}] {name}")

    # Shuffle the test columns to prove order independence.
    shuffled_test = {
        "plan_tier": test["plan_tier"], "level": test["level"],
        "score": test["score"], "city": test["city"],
        "income": test["income"], "age": test["age"],
    }

    result_canonical = pipeline.transform(test)
    result_shuffled = pipeline.transform(shuffled_test)
    assert np.allclose(result_canonical.X, result_shuffled.X), \
        "shuffled-column inference must be identical"

    print("\n" + "=" * 72)
    print("TRANSFORM of 6 TEST rows (unknown city 'Hangzhou', missing, const col)")
    print("=" * 72)
    np.set_printoptions(precision=4, suppress=True, linewidth=120)
    print(result_canonical.X)
    print("\nUnknown-city flag per row:",
          result_canonical.unknown_categories["city"].tolist())
    print("Unknown-level flag per row:",
          result_canonical.unknown_categories["level"].tolist())
    assert result_canonical.unknown_categories["city"][0]
    # Zero-variance column: mean=7, scale fallback=1 -> all zeros.
    plan_idx = result_canonical.feature_names_out.index("plan_tier")
    assert np.allclose(result_canonical.X[:, plan_idx], 0.0)
    print("Zero-variance 'plan_tier' column -> all zeros (no divide-by-zero). OK")

    # Save / load parity.
    model_path = outdir / "pipeline.json"
    save_pipeline(pipeline, model_path)
    restored = load_pipeline(model_path)
    result_loaded = restored.transform(shuffled_test)
    assert np.array_equal(result_loaded.X, result_canonical.X), \
        "save/load must reproduce identical output"
    assert restored.feature_names_out == pipeline.feature_names_out
    print(f"\nSave -> load parity verified against {model_path}")

    # Emit example artifacts for the request samples.
    _write_json(outdir / "spec.json", [
        {k: v for k, v in spec.__dict__.items()} for spec in specs
    ])
    _write_json(outdir / "train_sample.json", _to_jsonable_data(
        {k: v[:8] for k, v in train.items()}))
    _write_json(outdir / "transform_request.json", _to_jsonable_data(shuffled_test))
    _write_json(outdir / "transform_response.json", {
        "feature_names_out": result_loaded.feature_names_out,
        "rows": result_loaded.X.tolist(),
        "unknown_categories": {
            k: m.tolist() for k, m in result_loaded.unknown_categories.items()
        },
    })
    print(f"Example request/response files written to {outdir}/")
    print("Demo finished successfully.")
    return result_loaded
