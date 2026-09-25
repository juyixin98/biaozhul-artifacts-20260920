"""JSON-file CLI service: local fit / predict infrastructure.

Requests and responses are plain JSON files, so the service has no network
surface and examples can be inspected with any editor.

Train request:
    {
      "schema": {"columns": [...], "target": "price"},
      "rows": [ {"age": 30, ...}, ... ],
      "targets": [12.5, ...]
    }

Infer request:
    { "rows": [ {"city": "north", "age": 31, ...}, ... ] }

CLI:
    python -m feature_pipeline.service train \\
        --request examples/train_request.json \\
        --artifact artifacts/model.json
    python -m feature_pipeline.service infer \\
        --artifact artifacts/model.json \\
        --request examples/infer_request.json \\
        --output artifacts/predictions.json

Column order inside infer rows is irrelevant: columns are read by name.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

from .artifact import ArtifactError, ModelArtifact
from .schema import Schema, SchemaError

TRAIN = "train"
INFER = "infer"


def load_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level JSON value must be an object")
    return data


def train_from_request(request: dict[str, Any]) -> tuple[ModelArtifact, dict[str, Any]]:
    """Fit an artifact from a parsed train request. Returns (artifact, report)."""
    schema = Schema.from_dict(request["schema"])
    rows = request["rows"]
    targets = request.get("targets")
    if schema.target is None:
        raise SchemaError("train request schema must declare a target")
    if not isinstance(rows, list) or not rows:
        raise SchemaError("rows must be a non-empty list")
    if not isinstance(targets, list) or len(targets) != len(rows):
        raise SchemaError(
            f"targets must be a list of length {len(rows)}, "
            f"got length {len(targets) if isinstance(targets, list) else 'n/a'}"
        )
    artifact = ModelArtifact.fit(schema, rows, targets)
    report = {
        "n_train": len(rows),
        "n_features_in": len(schema.feature_names),
        "feature_names_out": artifact.feature_names,
        "model_rank": artifact.model.rank_,
        "intercept": artifact.model.intercept_,
        "columns_in_order": schema.feature_names,
    }
    return artifact, report


def infer_from_request(
    artifact: ModelArtifact, request: dict[str, Any]
) -> dict[str, Any]:
    """Run read-only inference from a parsed infer request."""
    rows = request.get("rows")
    if not isinstance(rows, list) or not rows:
        raise SchemaError("infer request requires a non-empty 'rows' list")
    predictions = artifact.predict_rows(rows)
    return {
        "columns_in_order": artifact.schema.feature_names,
        "feature_names_out": artifact.feature_names,
        "n_rows": len(rows),
        "predictions": [float(p) for p in predictions],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="feature_pipeline.service",
        description="Local feature-transformation pipeline service",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    train_parser = sub.add_parser(TRAIN, help="fit pipeline + model and save artifact")
    train_parser.add_argument("--request", required=True, help="train request JSON")
    train_parser.add_argument("--artifact", required=True, help="output artifact path")
    train_parser.add_argument(
        "--report", help="optional path for the fit report JSON"
    )

    infer_parser = sub.add_parser(INFER, help="load artifact and predict")
    infer_parser.add_argument("--artifact", required=True, help="saved artifact JSON")
    infer_parser.add_argument("--request", required=True, help="infer request JSON")
    infer_parser.add_argument("--output", help="output predictions JSON (stdout if omitted)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == TRAIN:
            request = load_json(args.request)
            artifact, report = train_from_request(request)
            saved_to = artifact.save(args.artifact)
            report["artifact_path"] = str(saved_to)
            report_text = json.dumps(report, indent=2, ensure_ascii=False)
            if args.report:
                Path(args.report).parent.mkdir(parents=True, exist_ok=True)
                Path(args.report).write_text(
                    report_text + "\n", encoding="utf-8"
                )
            print(report_text)
            return 0

        request = load_json(args.request)
        artifact = ModelArtifact.load(args.artifact)
        response = infer_from_request(artifact, request)
        response_text = json.dumps(response, indent=2, ensure_ascii=False)
        if args.output:
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(response_text + "\n", encoding="utf-8")
        print(response_text)
        return 0
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError,
            SchemaError, ArtifactError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
