"""Reproducible synthetic dataset for end-to-end verification.

No external data is downloaded: everything is generated from a seeded
numpy Generator. The dataset deliberately contains the hard cases the
acceptance criteria require:

* missing values in numeric and categorical columns
* an exactly zero-variance numeric column
* a categorical value in the test split that was never seen in training
* test rows emitted with shuffled key order (columns out of order)

The target is a known linear function of the (expanded) features plus
Gaussian noise, so a correct pipeline + OLS model can be checked against
ground-truth coefficients.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .schema import CATEGORICAL, NUMERIC, ColumnSpec, Schema

CITIES = ("north", "south", "east", "west")
UNKNOWN_CITY = "remote"
CITY_EFFECTS = {"north": 10.0, "south": -5.0, "east": 2.0, "west": -8.0}

TRUE_COEFFICIENTS = {
    "age": 3.0,
    "income": 0.05,
    "constant_col": 0.0,  # zero variance: contributes the intercept only
    # categorical effects applied by name during generation
    "city": CITY_EFFECTS,
}
TRUE_INTERCEPT = 50.0
NOISE_STD = 0.5


def build_schema(target: str = "price") -> Schema:
    """Schema used by the synthetic dataset."""
    return Schema(
        columns=(
            ColumnSpec("age", NUMERIC, impute="mean"),
            ColumnSpec("income", NUMERIC, impute="median"),
            ColumnSpec("constant_col", NUMERIC, impute="constant", fill_value=7.0),
            ColumnSpec("city", CATEGORICAL, impute="mode"),
        ),
        target=target,
    )


def _generate_split(
    rng: np.random.Generator,
    n_rows: int,
    *,
    include_unknown_category: bool,
    shuffle_key_order: bool,
    missing_rate: float,
) -> tuple[list[dict[str, Any]], np.ndarray]:
    age = rng.normal(loc=35.0, scale=8.0, size=n_rows)
    income = rng.normal(loc=5000.0, scale=900.0, size=n_rows)
    constant = np.full(n_rows, 7.0)
    city_idx = rng.integers(0, len(CITIES), size=n_rows)
    # dtype=object is required: a fixed-width unicode array (<U5 here) would
    # silently truncate the longer "remote" unknown category to "remot".
    cities = np.array([CITIES[i] for i in city_idx], dtype=object)
    if include_unknown_category and n_rows > 0:
        cities[-1] = UNKNOWN_CITY
        city_idx[-1] = -1  # marker: unknown category

    target = (
        TRUE_INTERCEPT
        + TRUE_COEFFICIENTS["age"] * age
        + TRUE_COEFFICIENTS["income"] * income
        + np.array([
            0.0 if idx == -1 else CITY_EFFECTS[CITIES[idx]]
            for idx in city_idx
        ])
        + rng.normal(0.0, NOISE_STD, size=n_rows)
    )

    age_obj: np.ndarray = age.astype(object)
    income_obj: np.ndarray = income.astype(object)
    cities_obj: np.ndarray = cities.astype(object)
    for pos in range(n_rows):
        if rng.random() < missing_rate:
            age_obj[pos] = None
        if rng.random() < missing_rate:
            income_obj[pos] = None
        # Never overwrite the injected unknown-category row: the unknown
        # value must survive into the test split.
        if city_idx[pos] != -1 and rng.random() < missing_rate * 0.5:
            cities_obj[pos] = None

    rows: list[dict[str, Any]] = []
    for pos in range(n_rows):
        row = {
            "age": (None if age_obj[pos] is None else float(age_obj[pos])),
            "income": (None if income_obj[pos] is None
                       else float(income_obj[pos])),
            "constant_col": float(constant[pos]),
            "city": (None if cities_obj[pos] is None
                     else str(cities_obj[pos])),
        }
        if shuffle_key_order:
            keys = list(row.keys())
            rng.shuffle(keys)
            row = {key: row[key] for key in keys}
        rows.append(row)
    return rows, target


def make_synthetic_dataset(
    n_train: int = 400,
    n_test: int = 50,
    seed: int = 20260925,
    missing_rate: float = 0.05,
) -> dict[str, Any]:
    """Generate disjoint train/test splits from independent RNG streams.

    Test data is generated with a *child* spawned from the seeded generator,
    and crucially the pipeline is never fit on it: callers fit on
    ``train_rows`` only.
    """
    if n_train <= 0 or n_test <= 0:
        raise ValueError("n_train and n_test must be positive")
    if not 0.0 <= missing_rate < 1.0:
        raise ValueError("missing_rate must be in [0, 1)")

    rng = np.random.default_rng(seed)
    train_rng, test_rng = rng.spawn(2)

    train_rows, train_target = _generate_split(
        train_rng, n_train,
        include_unknown_category=False,
        shuffle_key_order=False,
        missing_rate=missing_rate,
    )
    test_rows, test_target = _generate_split(
        test_rng, n_test,
        include_unknown_category=True,
        shuffle_key_order=True,
        missing_rate=missing_rate,
    )
    schema = build_schema()
    return {
        "schema": schema,
        "train_rows": train_rows,
        "train_targets": train_target.tolist(),
        "test_rows": test_rows,
        "test_targets": test_target.tolist(),
        "known_categories": list(CITIES),
        "unknown_category": UNKNOWN_CITY,
        "true_coefficients": {
            "intercept": TRUE_INTERCEPT,
            "age": TRUE_COEFFICIENTS["age"],
            "income": TRUE_COEFFICIENTS["income"],
            "city_effects": dict(CITY_EFFECTS),
        },
    }
