"""Tests for the JSON command-line interface."""
import json
from pathlib import Path

import numpy as np
import pytest

from feature_pipeline.cli import main

SPECS = [
    {"name": "age", "dtype": "numeric", "impute_strategy": "mean", "scale": True},
    {"name": "city", "dtype": "categorical",
     "impute_strategy": "most_frequent", "handle_unknown": "indicator"},
]
TRAIN = {
    "age": [10.0, 20.0, None, 40.0],
    "city": ["NY", "SF", "NY", "SF"],
}
REQUEST = {
    # columns deliberately out of order, plus an unseen category and a missing value
    "city": ["TKY", None, "NY"],
    "age": [None, 50.0, 15.0],
}


def _write(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_cli_fit_then_transform_roundtrip(tmp_path, capsys):
    spec = _write(tmp_path / "spec.json", SPECS)
    data = _write(tmp_path / "train.json", TRAIN)
    model = tmp_path / "model.json"

    assert main(["fit", "--spec", str(spec), "--data", str(data),
                 "--out", str(model)]) == 0
    assert model.exists()
    assert "Output features" in capsys.readouterr().out

    request = _write(tmp_path / "request.json", REQUEST)
    assert main(["transform", "--model", str(model),
                 "--data", str(request)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["feature_names_out"] == [
        "age", "city=NY", "city=SF", "city=__unknown__"]
    # Row 0 carries the unknown-city indicator.
    assert printed["rows"][0][3] == 1.0
    assert printed["unknown_categories"]["city"] == [True, False, False]


def test_cli_transform_to_file(tmp_path):
    spec = _write(tmp_path / "spec.json", SPECS)
    data = _write(tmp_path / "train.json", TRAIN)
    model = tmp_path / "model.json"
    main(["fit", "--spec", str(spec), "--data", str(data), "--out", str(model)])

    request = _write(tmp_path / "request.json", REQUEST)
    out = tmp_path / "result.json"
    assert main(["transform", "--model", str(model), "--data", str(request),
                 "--out", str(out)]) == 0
    assert json.loads(out.read_text())["rows"]


def test_cli_missing_data_file_exits_nonzero(tmp_path):
    spec = _write(tmp_path / "spec.json", SPECS)
    model = tmp_path / "model.json"
    main(["fit", "--spec", str(spec),
          "--data", str(_write(tmp_path / "t.json", TRAIN)),
          "--out", str(model)])
    with pytest.raises(SystemExit) as exc:
        main(["transform", "--model", str(model),
              "--data", str(tmp_path / "ghost.json")])
    assert exc.value.code == 2


def test_cli_demo_writes_artifacts(tmp_path):
    assert main(["demo", "--outdir", str(tmp_path)]) == 0
    for name in ("pipeline.json", "spec.json", "train_sample.json",
                 "transform_request.json", "transform_response.json"):
        assert (tmp_path / name).exists()
