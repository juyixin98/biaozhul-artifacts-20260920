"""Tests for the ordered Pipeline, including column-order tolerance."""

import numpy as np
import pytest

from feature_pipeline.errors import NotFittedError
from feature_pipeline.pipeline import Pipeline
from feature_pipeline.schema import CATEGORICAL, NUMERIC, ColumnSpec, Schema


def make_schema() -> Schema:
    return Schema(
        columns=(
            ColumnSpec("age", NUMERIC, impute="mean"),
            ColumnSpec("city", CATEGORICAL, impute="mode"),
        ),
        target="price",
    )


def make_rows():
    return [
        {"age": 20.0, "city": "north"},
        {"age": 40.0, "city": "south"},
        {"age": None, "city": "north"},
        {"age": 30.0, "city": None},
    ]


class TestPipelineFitTransform:
    def test_transform_before_fit_raises(self):
        pipeline = Pipeline(make_schema())
        with pytest.raises(NotFittedError):
            pipeline.transform(make_rows())

    def test_output_column_order_follows_schema_not_input(self):
        pipeline = Pipeline(make_schema()).fit(make_rows())
        # age -> 1 column; city {north, south} -> 2 columns => 3 features
        assert pipeline.feature_names_out == [
            "age", "city=north", "city=south"
        ]
        matrix = pipeline.transform(make_rows())
        assert matrix.shape == (4, 3)

    def test_shuffled_input_keys_produce_identical_matrix(self):
        pipeline = Pipeline(make_schema()).fit(make_rows())
        ordered = pipeline.transform(make_rows())
        shuffled_rows = [
            dict(reversed(list(row.items()))) for row in make_rows()
        ]
        # And add an extra key that must be ignored.
        shuffled_rows = [
            {**row, "extra": "ignore-me"} for row in shuffled_rows
        ]
        shuffled = pipeline.transform(shuffled_rows)
        np.testing.assert_allclose(ordered, shuffled)

    def test_missing_column_at_inference_raises(self):
        pipeline = Pipeline(make_schema()).fit(make_rows())
        with pytest.raises(ValueError, match="missing columns"):
            pipeline.transform([{"age": 25.0}])

    def test_pipeline_round_trips_through_dict(self):
        pipeline = Pipeline(make_schema()).fit(make_rows())
        restored = Pipeline.from_dict(pipeline.to_dict())
        original = pipeline.transform(make_rows())
        loaded = restored.transform(make_rows())
        np.testing.assert_allclose(original, loaded)
        assert restored.feature_names_out == pipeline.feature_names_out

    def test_test_rows_are_never_used_for_fit_statistics(self):
        # Training age values are all 10; test contains value 1000.
        train_rows = [
            {"age": 10.0, "city": "a"},
            {"age": 10.0, "city": "b"},
        ]
        pipeline = Pipeline(make_schema()).fit(train_rows)
        age_mean = pipeline.steps("age")[0].statistic_
        assert age_mean == 10.0
        test_rows = [
            {"age": 1000.0, "city": "a"},
            {"age": None, "city": "unseen"},
        ]
        matrix = pipeline.transform(test_rows)
        # Missing test value is filled with the TRAINING mean (10), not 1000.
        np.testing.assert_allclose(matrix[1, 0], 0.0, atol=1e-12)
