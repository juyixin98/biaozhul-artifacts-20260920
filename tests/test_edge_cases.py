"""Edge-case coverage: spec validation, malformed input, defensive branches."""
import json

import numpy as np
import pytest

from feature_pipeline import (
    ColumnSpec,
    FeaturePipeline,
    OneHotEncoder,
    SimpleImputer,
    StandardScaler,
    load_pipeline,
    save_pipeline,
)
from feature_pipeline.exceptions import (
    FittingError,
    SchemaError,
    SerializationError,
)


# --------------------------------------------------------------- ColumnSpec #
def test_spec_rejects_unknown_dtype():
    with pytest.raises(SchemaError):
        ColumnSpec(name="x", dtype="timestamp")


def test_spec_rejects_unknown_strategy():
    with pytest.raises(SchemaError):
        ColumnSpec(name="x", dtype="numeric", impute_strategy="mode")


def test_spec_rejects_strategy_dtype_mismatch():
    with pytest.raises(SchemaError):
        ColumnSpec(name="x", dtype="numeric", impute_strategy="most_frequent")
    with pytest.raises(SchemaError):
        ColumnSpec(name="x", dtype="categorical", impute_strategy="mean")


def test_pipeline_requires_columns_and_unique_names():
    with pytest.raises(SchemaError):
        FeaturePipeline([])
    with pytest.raises(SchemaError):
        FeaturePipeline([
            ColumnSpec("a", "numeric"), ColumnSpec("a", "numeric")
        ])


# --------------------------------------------------------------- imputer --- #
def test_imputer_rejects_bad_strategy_and_constant_without_value():
    with pytest.raises(FittingError):
        SimpleImputer(strategy="banana")
    with pytest.raises(FittingError):
        SimpleImputer(strategy="constant")


def test_imputer_non_nan_float_is_not_missing():
    # is_missing must not treat ordinary floats (incl. large ones) as missing.
    imp = SimpleImputer(strategy="mean").fit(np.array([1.0, 2.0]))
    np.testing.assert_allclose(imp.transform(np.array([3.0])), [3.0])


# --------------------------------------------------------------- scaler ---- #
def test_scaler_unfitted_n_columns_access_guarded():
    with pytest.raises(Exception):
        OneHotEncoder().n_columns_out_


# --------------------------------------------------------------- encoder --- #
def test_encoder_rejects_bad_handle_unknown():
    with pytest.raises(FittingError):
        OneHotEncoder(handle_unknown="explode")


def test_encoder_all_missing_categorical_fails():
    with pytest.raises(FittingError):
        OneHotEncoder().fit(np.array([None, None], dtype=object))


def test_encoder_ignore_mode_unknown_is_all_zero_no_flag():
    enc = OneHotEncoder(handle_unknown="ignore").fit(
        np.array(["a"], dtype=object))
    out, unknown = enc.transform(np.array(["zzz"], dtype=object))
    assert out.shape == (1, 1)
    np.testing.assert_allclose(out, [[0.0]])
    assert unknown.tolist() == [True]


# --------------------------------------------------------------- schema ---- #
def _fitted():
    return FeaturePipeline([
        ColumnSpec("n", "numeric", impute_strategy="mean"),
        ColumnSpec("c", "categorical", impute_strategy="most_frequent"),
    ]).fit({
        "n": np.array([1.0, 2.0]),
        "c": np.array(["a", "b"], dtype=object),
    })


def test_transform_requires_mapping():
    pipe = _fitted()
    with pytest.raises(SchemaError):
        pipe.transform([1.0, 2.0])


def test_transform_rejects_multidimensional_column():
    pipe = _fitted()
    with pytest.raises(SchemaError):
        pipe.transform({
            "n": np.array([[1.0, 2.0], [3.0, 4.0]]),
            "c": np.array(["a", "b"], dtype=object),
        })


def test_fit_rejects_non_numeric_numeric_column():
    pipe = FeaturePipeline([ColumnSpec("n", "numeric",
                                       impute_strategy="constant", fill_value=0.0)])
    with pytest.raises(FittingError):
        pipe.fit({"n": np.array(["oops", "nope"], dtype=object)})


def test_fit_rejects_entirely_missing_column():
    pipe = FeaturePipeline([
        ColumnSpec("n", "numeric", impute_strategy="constant", fill_value=1.0)
    ])
    with pytest.raises(FittingError):
        pipe.fit({"n": np.array([np.nan, np.nan])})


# --------------------------------------------------------------- serialization #
def test_load_rejects_non_json_and_non_object(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(SerializationError):
        load_pipeline(bad)

    bad.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(SerializationError):
        load_pipeline(bad)


def test_load_rejects_unknown_format(tmp_path):
    pipe = _fitted()
    path = tmp_path / "m.json"
    save_pipeline(pipe, path)
    doc = json.loads(path.read_text())
    doc["format"] = "something-else"
    # Re-sign so the checksum passes and format validation is what fails.
    import hashlib
    import feature_pipeline.serialization as ser
    unsigned = {k: v for k, v in doc.items() if k != "checksum"}
    doc["checksum"] = hashlib.sha256(ser._canonical(unsigned)).hexdigest()
    path.write_text(json.dumps(doc))
    with pytest.raises(SerializationError):
        load_pipeline(path)


def test_nan_constant_fill_roundtrips(tmp_path):
    pipe = FeaturePipeline([
        ColumnSpec("n", "numeric", impute_strategy="constant",
                   fill_value=float("nan"), scale=False)
    ]).fit({"n": np.array([1.0, 2.0])})
    path = tmp_path / "m.json"
    save_pipeline(pipe, path)
    restored = load_pipeline(path)
    out = restored.transform({"n": np.array([np.nan, 1.0])})
    np.testing.assert_allclose(out.X[:, 0], [np.nan, 1.0])
