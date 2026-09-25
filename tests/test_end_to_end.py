"""End-to-end acceptance tests on reproducible synthetic data.

Covers the four acceptance scenarios:
1. test data never participates in fitting;
2. unknown categorical values at inference;
3. columns arriving out of order;
4. zero-variance numeric columns;
plus save/load prediction parity and model sanity against ground truth.
"""

import numpy as np

from feature_pipeline.artifact import ModelArtifact
from feature_pipeline.data import make_synthetic_dataset
from feature_pipeline.transforms import StandardScaler


def fit_on_train() -> tuple[ModelArtifact, dict]:
    dataset = make_synthetic_dataset()
    artifact = ModelArtifact.fit(
        dataset["schema"],
        dataset["train_rows"],
        dataset["train_targets"],
    )
    return artifact, dataset


class TestSyntheticEndToEnd:
    def test_test_split_contains_unknown_category_not_in_training(self):
        _, dataset = fit_on_train()
        train_cities = {
            row["city"] for row in dataset["train_rows"] if row["city"]
        }
        test_cities = {
            row["city"] for row in dataset["test_rows"] if row["city"]
        }
        assert dataset["unknown_category"] in test_cities
        assert dataset["unknown_category"] not in train_cities

    def test_unknown_category_encodes_to_all_zeros(self):
        artifact, dataset = fit_on_train()
        unknown_rows = [
            row for row in dataset["test_rows"]
            if row.get("city") == dataset["unknown_category"]
        ]
        assert unknown_rows, "synthetic test split must include unknown city"
        matrix = artifact.pipeline.transform(unknown_rows)
        city_feature_count = len(dataset["known_categories"])
        city_block = matrix[:, -city_feature_count:]
        np.testing.assert_array_equal(city_block, np.zeros_like(city_block))

    def test_shuffled_test_key_order_matches_canonical_order(self):
        artifact, dataset = fit_on_train()
        canonical = [
            {name: row[name] for name in dataset["schema"].feature_names}
            for row in dataset["test_rows"]
        ]
        matrix_shuffled = artifact.pipeline.transform(dataset["test_rows"])
        matrix_canonical = artifact.pipeline.transform(canonical)
        np.testing.assert_allclose(matrix_shuffled, matrix_canonical)

    def test_zero_variance_column_is_detected_and_scaled_to_zero(self):
        artifact, _ = fit_on_train()
        scaler = artifact.pipeline.steps("constant_col")[1]
        assert isinstance(scaler, StandardScaler)
        assert scaler.zero_variance_ is True
        matrix = artifact.pipeline.transform([
            {"age": 1.0, "income": 2.0, "constant_col": 7.0, "city": "north"},
            {"age": 1.0, "income": 2.0, "constant_col": 7.0, "city": "south"},
        ])
        # constant_col is the 3rd feature; must be exactly zero.
        np.testing.assert_allclose(matrix[:, 2], [0.0, 0.0])

    def test_predictions_generalize_to_complete_test_rows(self):
        artifact, dataset = fit_on_train()
        known = set(dataset["known_categories"])
        complete_idx = [
            i for i, row in enumerate(dataset["test_rows"])
            if row["age"] is not None
            and row["income"] is not None
            and row["city"] in known
        ]
        assert complete_idx, "expected some complete, known-category test rows"
        rows = [dataset["test_rows"][i] for i in complete_idx]
        truth = np.array([dataset["test_targets"][i] for i in complete_idx])
        predictions = artifact.predict_rows(rows)
        # On complete rows the only error source is the 0.5-std noise;
        # allow margin for finite-sample fitting.
        rmse = float(np.sqrt(np.mean((predictions - truth) ** 2)))
        assert rmse < 3.0, f"unexpectedly high RMSE on complete rows: {rmse}"
        assert np.isfinite(predictions).all()

    def test_missing_value_rows_still_produce_finite_predictions(self):
        artifact, dataset = fit_on_train()
        missing_idx = [
            i for i, row in enumerate(dataset["test_rows"])
            if row["age"] is None or row["income"] is None
            or row["city"] is None
        ]
        assert missing_idx, "synthetic split should contain missing rows"
        rows = [dataset["test_rows"][i] for i in missing_idx]
        predictions = artifact.predict_rows(rows)
        assert np.isfinite(predictions).all()

    def test_training_statistics_come_from_train_rows_only(self):
        artifact, dataset = fit_on_train()
        # Independently recompute statistics from raw TRAIN data.
        train_age = np.array(
            [r["age"] for r in dataset["train_rows"] if r["age"] is not None]
        )
        train_income = np.array(
            [r["income"] for r in dataset["train_rows"]
             if r["income"] is not None]
        )
        age_imputer = artifact.pipeline.steps("age")[0]
        income_imputer = artifact.pipeline.steps("income")[0]
        np.testing.assert_allclose(age_imputer.statistic_, train_age.mean())
        np.testing.assert_allclose(
            income_imputer.statistic_, np.median(train_income)
        )
        # A missing value at inference is first filled with the TRAINING
        # mean; after standardization such a value is exactly zero.
        row = [{"age": None, "income": None, "constant_col": 7.0,
                "city": "north"}]
        matrix = artifact.pipeline.transform(row)
        np.testing.assert_allclose(matrix[0, 0], 0.0, atol=1e-12)

    def test_save_load_round_trip_matches_in_memory_predictions(self, tmp_path):
        artifact, dataset = fit_on_train()
        path = tmp_path / "artifact.json"
        artifact.save(path)
        reloaded = ModelArtifact.load(path)
        np.testing.assert_allclose(
            reloaded.predict_rows(dataset["test_rows"]),
            artifact.predict_rows(dataset["test_rows"]),
            atol=1e-12,
        )

    def test_dataset_is_reproducible_across_seeds(self):
        first = make_synthetic_dataset(seed=42)
        second = make_synthetic_dataset(seed=42)
        assert first["train_rows"] == second["train_rows"]
        assert first["test_targets"] == second["test_targets"]
