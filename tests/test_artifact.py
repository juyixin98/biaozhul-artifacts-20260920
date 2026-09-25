"""Tests for artifact serialization: save/load consistency and schema checks."""

import json
from pathlib import Path

import numpy as np
import pytest

from feature_pipeline.artifact import ARTIFACT_VERSION, ArtifactError, ModelArtifact
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


def make_fit_payload():
    rows = [
        {"age": 20.0, "city": "north"},
        {"age": 30.0, "city": "south"},
        {"age": None, "city": "north"},
        {"age": 50.0, "city": None},
    ]
    targets = [100.0, 120.0, 105.0, 90.0]
    return rows, targets


def fit_artifact() -> ModelArtifact:
    rows, targets = make_fit_payload()
    return ModelArtifact.fit(make_schema(), rows, targets)


class TestArtifactSaveLoad:
    def test_save_creates_valid_json_file(self, tmp_path: Path):
        path = tmp_path / "model.json"
        fit_artifact().save(path)
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["version"] == ARTIFACT_VERSION
        assert data["kind"] == "feature_pipeline_artifact"

    def test_loaded_artifact_predicts_identical_values(self, tmp_path: Path):
        artifact = fit_artifact()
        path = tmp_path / "model.json"
        artifact.save(path)
        loaded = ModelArtifact.load(path)

        infer_rows = [
            {"age": 25.0, "city": "north"},
            {"age": None, "city": "never-seen"},
            {"city": "south", "age": 99.0},  # shuffled keys
        ]
        np.testing.assert_allclose(
            artifact.predict_rows(infer_rows),
            loaded.predict_rows(infer_rows),
            atol=1e-12,
        )

    def test_feature_names_persisted_in_schema_order(self, tmp_path: Path):
        path = tmp_path / "model.json"
        fit_artifact().save(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        # Numeric column first, then sorted one-hot categories.
        assert data["feature_names"] == [
            "age", "city=north", "city=south"
        ]
        # Step order on disk follows schema column order.
        assert [s["column"] for s in data["pipeline"]["steps"]] == [
            "age", "city"
        ]

    def test_transformers_on_disk_are_fitted(self, tmp_path: Path):
        path = tmp_path / "model.json"
        fit_artifact().save(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        for step in data["pipeline"]["steps"]:
            assert all(t["fitted"] for t in step["transformers"])

    def test_loaded_artifact_does_not_refit_on_inference(self, tmp_path: Path):
        path = tmp_path / "model.json"
        fit_artifact().save(path)
        loaded = ModelArtifact.load(path)
        age_imputer = loaded.pipeline.steps("age")[0]
        statistic_before = age_imputer.statistic_
        loaded.predict_rows([{"age": 9999.0, "city": "north"}])
        assert age_imputer.statistic_ == statistic_before

    def test_rejects_wrong_kind(self, tmp_path: Path):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"kind": "something-else", "version": 1}),
                       encoding="utf-8")
        with pytest.raises(ArtifactError, match="not an artifact"):
            ModelArtifact.load(bad)

    def test_rejects_unsupported_version(self, tmp_path: Path):
        artifact = fit_artifact()
        data = artifact.to_dict()
        data["version"] = 999
        with pytest.raises(ArtifactError, match="unsupported artifact version"):
            ModelArtifact.from_dict(data)

    def test_rejects_feature_name_mismatch(self):
        data = fit_artifact().to_dict()
        data["feature_names"] = ["tampered", "names"]
        with pytest.raises(ArtifactError, match="feature names do not match"):
            ModelArtifact.from_dict(data)

    def test_fit_requires_target(self):
        schema = Schema(columns=(ColumnSpec("age", NUMERIC),))
        with pytest.raises(ValueError, match="schema.target"):
            ModelArtifact.fit(schema, [{"age": 1.0}], [1.0])
