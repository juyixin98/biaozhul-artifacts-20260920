"""Tests for pipeline serialization: order, schema, and save/load parity."""
import json
from pathlib import Path

import numpy as np
import pytest

from feature_pipeline import ColumnSpec, FeaturePipeline, load_pipeline, save_pipeline
from feature_pipeline.exceptions import SerializationError

SPECS = [
    ColumnSpec(name="age", dtype="numeric", impute_strategy="mean", scale=True),
    ColumnSpec(name="city", dtype="categorical", impute_strategy="most_frequent",
               handle_unknown="indicator"),
    ColumnSpec(name="rank", dtype="numeric", impute_strategy="constant",
               fill_value=-1.0, scale=False),
]

TRAIN = {
    "age": np.array([20.0, 40.0, np.nan, 60.0]),
    "city": np.array(["NY", "SF", "NY", "LA"], dtype=object),
    "rank": np.array([1.0, 2.0, 3.0, 4.0]),
}

TEST = {
    "age": np.array([np.nan, 30.0]),
    # deliberately shuffled order + an unseen category "TOKYO"
    "rank": np.array([9.0, np.nan]),
    "city": np.array(["TOKYO", "SF"], dtype=object),
}


def fit_pipeline():
    return FeaturePipeline(SPECS).fit(TRAIN)


def test_saved_json_records_version_schema_and_step_order(tmp_path):
    pipe = fit_pipeline()
    path = tmp_path / "model.json"
    save_pipeline(pipe, path)

    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["format"] == "feature-pipeline"
    assert doc["version"] == 1
    assert [c["name"] for c in doc["schema"]] == ["age", "city", "rank"]
    # Each step name records the deterministic per-column order: impute -> encode/scale.
    city_steps = [s["step"] for s in next(
        c for c in doc["columns"] if c["name"] == "city")["steps"]]
    assert city_steps == ["impute", "one_hot_encode"]
    age_steps = [s["step"] for s in next(
        c for c in doc["columns"] if c["name"] == "age")["steps"]]
    assert age_steps == ["impute", "standard_scale"]


def test_save_load_roundtrip_produces_identical_inference(tmp_path):
    pipe = fit_pipeline()
    expected = pipe.transform(TEST)

    path = tmp_path / "model.json"
    save_pipeline(pipe, path)
    restored = load_pipeline(path)

    assert restored.feature_names_out == pipe.feature_names_out
    result = restored.transform(TEST)
    np.testing.assert_allclose(result.X, expected.X)
    assert result.unknown_categories.keys() == expected.unknown_categories.keys()
    for name in expected.unknown_categories:
        np.testing.assert_array_equal(
            result.unknown_categories[name], expected.unknown_categories[name]
        )


def test_loaded_pipeline_is_read_only_and_matches_on_shuffled_columns(tmp_path):
    path = tmp_path / "model.json"
    save_pipeline(fit_pipeline(), path)
    restored = load_pipeline(path)

    shuffled = {
        "rank": TEST["rank"],
        "age": TEST["age"],
        "city": TEST["city"],
    }
    canonical = restored.transform(TEST).X
    np.testing.assert_allclose(restored.transform(shuffled).X, canonical)


def test_load_rejects_tampered_artifact(tmp_path):
    pipe = fit_pipeline()
    path = tmp_path / "model.json"
    save_pipeline(pipe, path)

    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["columns"][0]["steps"][0]["params"]["fill_value"] = 9999.0  # tamper
    path.write_text(json.dumps(doc), encoding="utf-8")

    with pytest.raises(SerializationError):
        load_pipeline(path)


def test_load_rejects_missing_file(tmp_path):
    with pytest.raises(SerializationError):
        load_pipeline(tmp_path / "nope.json")


def test_transforming_unfitted_pipeline_fails_before_any_save(tmp_path):
    # A pipeline that was never fitted cannot be serialized.
    with pytest.raises(SerializationError):
        save_pipeline(FeaturePipeline(SPECS), tmp_path / "x.json")
