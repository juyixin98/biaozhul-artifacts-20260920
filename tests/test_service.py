"""Tests for the JSON-file CLI service (train/infer, including error paths)."""

import json
from pathlib import Path

import numpy as np
import pytest

from feature_pipeline.service import infer_from_request, main, train_from_request
from feature_pipeline.data import make_synthetic_dataset


def write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def train_payload() -> dict:
    dataset = make_synthetic_dataset(n_train=120, n_test=10)
    return {
        "schema": dataset["schema"].to_dict(),
        "rows": dataset["train_rows"],
        "targets": dataset["train_targets"],
    }


class TestTrainService:
    def test_train_report_lists_schema_order_and_features(self):
        artifact, report = train_from_request(train_payload())
        assert report["n_train"] == 120
        assert report["columns_in_order"] == [
            "age", "income", "constant_col", "city"
        ]
        assert report["feature_names_out"][:3] == [
            "age", "income", "constant_col"
        ]
        assert artifact.model.is_fitted

    def test_target_count_mismatch_is_rejected(self):
        payload = train_payload()
        payload["targets"] = payload["targets"][:-1]
        with pytest.raises(ValueError, match="targets must be a list"):
            train_from_request(payload)

    def test_schema_without_target_is_rejected(self):
        payload = train_payload()
        payload["schema"]["target"] = None
        with pytest.raises(ValueError, match="declare a target"):
            train_from_request(payload)


class TestInferService:
    def test_infer_tolerates_shuffled_keys_and_extra_keys(self):
        artifact, _ = train_from_request(train_payload())
        rows = [
            {"city": "north", "constant_col": 7.0, "income": 5000.0,
             "age": 30.0, "note": "extra"},
            {"age": 40.0, "city": "unseen-borough", "constant_col": 7.0,
             "income": None},
        ]
        response = infer_from_request(artifact, {"rows": rows})
        assert response["n_rows"] == 2
        assert len(response["predictions"]) == 2
        assert all(np.isfinite(response["predictions"]))

    def test_infer_requires_rows(self):
        artifact, _ = train_from_request(train_payload())
        with pytest.raises(ValueError, match="'rows'"):
            infer_from_request(artifact, {})


class TestCli:
    def test_train_then_infer_round_trip(self, tmp_path: Path):
        request_path = write_json(tmp_path / "train.json", train_payload())
        artifact_path = tmp_path / "artifact.json"
        report_path = tmp_path / "report.json"

        rc = main([
            "train", "--request", str(request_path),
            "--artifact", str(artifact_path),
            "--report", str(report_path),
        ])
        assert rc == 0
        assert artifact_path.exists()
        assert report_path.exists()

        infer_rows = [
            {"city": "south", "age": 33.0, "income": 4800.0,
             "constant_col": 7.0},
            {"age": None, "income": None, "constant_col": 7.0,
             "city": "nowhere"},
        ]
        infer_path = write_json(tmp_path / "infer.json", {"rows": infer_rows})
        output_path = tmp_path / "predictions.json"
        rc = main([
            "infer", "--artifact", str(artifact_path),
            "--request", str(infer_path),
            "--output", str(output_path),
        ])
        assert rc == 0
        response = json.loads(output_path.read_text(encoding="utf-8"))
        assert len(response["predictions"]) == 2

    def test_missing_request_file_returns_error_code(self, tmp_path: Path):
        rc = main([
            "train", "--request", str(tmp_path / "nope.json"),
            "--artifact", str(tmp_path / "a.json"),
        ])
        assert rc == 1

    def test_malformed_json_returns_error_code(self, tmp_path: Path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json", encoding="utf-8")
        rc = main([
            "train", "--request", str(bad),
            "--artifact", str(tmp_path / "a.json"),
        ])
        assert rc == 1
