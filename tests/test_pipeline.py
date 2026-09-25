"""Tests for the end-to-end FeaturePipeline: schema, ordering, leakage, read-only inference."""
import numpy as np
import pytest

from feature_pipeline import ColumnSpec, FeaturePipeline
from feature_pipeline.exceptions import NotFittedError, SchemaError

SPECS = [
    ColumnSpec(name="age", dtype="numeric", impute_strategy="mean"),
    ColumnSpec(name="city", dtype="categorical", impute_strategy="most_frequent"),
    ColumnSpec(name="income", dtype="numeric", impute_strategy="constant",
               fill_value=0.0, scale=True),
]


def make_pipeline():
    return FeaturePipeline(SPECS)


def test_pipeline_fit_then_transform_outputs_fixed_schema_order():
    pipe = make_pipeline()
    train = {
        "age": np.array([20.0, 40.0, np.nan]),
        "city": np.array(["NY", "SF", "NY"], dtype=object),
        "income": np.array([100.0, 300.0, 200.0]),
    }
    pipe.fit(train)

    assert pipe.feature_names_out == [
        "age",
        "city=NY",
        "city=SF",
        "city=__unknown__",
        "income",
    ]
    out = pipe.transform(train)
    assert out.X.shape == (3, 5)


def test_test_data_does_not_reaffect_fitted_state_and_unknown_category_handled():
    pipe = make_pipeline()
    train = {
        "age": np.array([0.0, 10.0]),
        "city": np.array(["a", "b"], dtype=object),
        "income": np.array([0.0, 10.0]),
    }
    pipe.fit(train)

    # Snapshot fitted statistics (tuple layout: spec, imputer, scaler/encoder).
    age_mean_before = pipe.transformers_["age"][2].mean_
    cities_before = list(pipe.transformers_["city"][2].categories_)

    test = {
        # 100 is far outside train range: must use train mean/std, not recompute.
        "age": np.array([100.0, np.nan]),
        # "zzz" is an unknown category at inference time.
        "city": np.array(["zzz", "a"], dtype=object),
        "income": np.array([np.nan, 5.0]),
    }
    result = pipe.transform(test)

    assert result.unknown_categories["city"].tolist() == [True, False]
    # (100-5)/5 = 19 ; NaN is imputed to train mean 5, then scaled to 0.
    np.testing.assert_allclose(result.X[:, 0], [19.0, 0.0])
    # Unknown row's one-hot + indicator, known row unaffected.
    np.testing.assert_allclose(result.X[0, 1:4], [0.0, 0.0, 1.0])
    np.testing.assert_allclose(result.X[1, 1:4], [1.0, 0.0, 0.0])
    # Fitted state untouched by transform.
    assert pipe.transformers_["age"][2].mean_ == age_mean_before
    assert list(pipe.transformers_["city"][2].categories_) == cities_before


def test_shuffled_columns_and_extra_rows_are_reordered_to_schema():
    pipe = make_pipeline()
    pipe.fit({
        "age": np.array([1.0, 2.0]),
        "city": np.array(["x", "y"], dtype=object),
        "income": np.array([1.0, 2.0]),
    })

    # Same rows supplied in a DIFFERENT column order; values must stay aligned.
    shuffled = {
        "income": np.array([2.0, 1.0]),
        "age": np.array([2.0, 1.0]),
        "city": np.array(["y", "x"], dtype=object),
    }
    ordered = pipe.transform(shuffled).X
    canonical = pipe.transform({
        "age": np.array([2.0, 1.0]),
        "city": np.array(["y", "x"], dtype=object),
        "income": np.array([2.0, 1.0]),
    }).X
    np.testing.assert_allclose(ordered, canonical)


def test_missing_column_raises_schema_error():
    pipe = make_pipeline()
    pipe.fit({
        "age": np.array([1.0]),
        "city": np.array(["x"], dtype=object),
        "income": np.array([1.0]),
    })
    with pytest.raises(SchemaError):
        pipe.transform({"age": np.array([1.0]), "city": np.array(["x"], dtype=object)})


def test_unexpected_extra_column_raises_schema_error_in_strict_mode():
    pipe = make_pipeline().fit({
        "age": np.array([1.0]),
        "city": np.array(["x"], dtype=object),
        "income": np.array([1.0]),
    })
    with pytest.raises(SchemaError):
        pipe.transform({
            "age": np.array([1.0]),
            "city": np.array(["x"], dtype=object),
            "income": np.array([1.0]),
            "ghost": np.array([1.0]),
        })


def test_ragged_row_counts_raise_schema_error():
    pipe = make_pipeline()
    pipe.fit({
        "age": np.array([1.0, 2.0]),
        "city": np.array(["x", "y"], dtype=object),
        "income": np.array([1.0, 2.0]),
    })
    with pytest.raises(SchemaError):
        pipe.transform({
            "age": np.array([1.0, 2.0, 3.0]),
            "city": np.array(["x", "y"], dtype=object),
            "income": np.array([1.0, 2.0]),
        })


def test_zero_variance_numeric_column_transforms_to_zeros():
    pipe = FeaturePipeline([
        ColumnSpec(name="const", dtype="numeric", impute_strategy="constant",
                   fill_value=5.0, scale=True),
    ])
    pipe.fit({"const": np.array([5.0, 5.0, 5.0])})
    result = pipe.transform({"const": np.array([5.0, 5.0, 9.0])})
    # const mean=5, zero variance -> scale falls back to 1; output (x-5)/1.
    np.testing.assert_allclose(result.X[:, 0], [0.0, 0.0, 4.0])


def test_transform_before_fit_raises():
    with pytest.raises(NotFittedError):
        make_pipeline().transform({"age": np.array([1.0])})


def test_fit_is_required_and_fit_transform_equivalent_to_fit_then_transform():
    train = {
        "age": np.array([1.0, np.nan, 3.0]),
        "city": np.array(["a", "b", "a"], dtype=object),
        "income": np.array([10.0, 20.0, 30.0]),
    }
    p1 = make_pipeline().fit(train)
    p2 = make_pipeline()
    np.testing.assert_allclose(p1.transform(train).X, p2.fit_transform(train).X)
