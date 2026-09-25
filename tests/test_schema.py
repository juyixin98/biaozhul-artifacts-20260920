"""Tests for schema definitions and validation."""

import pytest

from feature_pipeline.schema import CATEGORICAL, NUMERIC, ColumnSpec, Schema, SchemaError


class TestColumnSpec:
    def test_defaults_numeric_impute_to_mean(self):
        spec = ColumnSpec("age", NUMERIC)
        assert spec.impute == "mean"

    def test_defaults_categorical_impute_to_mode(self):
        spec = ColumnSpec("city", CATEGORICAL)
        assert spec.impute == "mode"

    def test_rejects_unknown_dtype(self):
        with pytest.raises(SchemaError, match="dtype must be"):
            ColumnSpec("bad", "boolean")

    def test_rejects_strategy_incompatible_with_dtype(self):
        with pytest.raises(SchemaError, match="not in"):
            ColumnSpec("age", NUMERIC, impute="mode")

    def test_constant_requires_fill_value(self):
        with pytest.raises(SchemaError, match="constant imputation requires"):
            ColumnSpec("age", NUMERIC, impute="constant")

    def test_rejects_non_numeric_fill_value(self):
        with pytest.raises(SchemaError, match="numeric fill_value"):
            ColumnSpec("age", NUMERIC, impute="constant", fill_value="oops")

    def test_round_trips_through_dict(self):
        spec = ColumnSpec("age", NUMERIC, impute="constant", fill_value=3.5)
        restored = ColumnSpec.from_dict(spec.to_dict())
        assert restored == spec


class TestSchema:
    def test_rejects_empty_schema(self):
        with pytest.raises(SchemaError, match="at least one column"):
            Schema(columns=())

    def test_rejects_duplicate_column_names(self):
        with pytest.raises(SchemaError, match="duplicate column"):
            Schema(columns=(
                ColumnSpec("age", NUMERIC),
                ColumnSpec("age", NUMERIC),
            ))

    def test_rejects_target_that_is_also_a_feature(self):
        with pytest.raises(SchemaError, match="must not also be"):
            Schema(columns=(ColumnSpec("price", NUMERIC),), target="price")

    def test_validate_rows_reports_missing_columns(self):
        schema = Schema(columns=(
            ColumnSpec("age", NUMERIC),
            ColumnSpec("city", CATEGORICAL),
        ))
        with pytest.raises(SchemaError, match="missing columns.*city"):
            schema.validate_rows([{"age": 30}])

    def test_validate_rows_rejects_non_object_rows(self):
        schema = Schema(columns=(ColumnSpec("age", NUMERIC),))
        with pytest.raises(SchemaError, match="must be an object"):
            schema.validate_rows([[30]])

    def test_extra_keys_in_rows_are_allowed(self):
        schema = Schema(columns=(ColumnSpec("age", NUMERIC),))
        schema.validate_rows([{"age": 30, "unexpected": "ignored"}])

    def test_round_trips_through_dict(self):
        schema = Schema(
            columns=(ColumnSpec("age", NUMERIC), ColumnSpec("city", CATEGORICAL)),
            target="price",
        )
        restored = Schema.from_dict(schema.to_dict())
        assert restored.feature_names == ["age", "city"]
        assert restored.target == "price"
