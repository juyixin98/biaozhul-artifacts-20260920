"""JSON serialization preserving step order, schema, and fitted state.

Artifact layout (deterministic key order)::

    {
      "format": "feature-pipeline", "version": 1,
      "schema":  [ {column spec}, ... ],          # declared schema, in order
      "columns": [ {"name": ..., "steps": [...] } | ... ],
      "feature_names_out": [...],
      "checksum": "<sha256 hex of the canonical JSON of the four fields above>"
    }

The checksum makes tampering detectable: load_pipeline() rejects any artifact
whose recomputed checksum does not match.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Union

from .exceptions import NotFittedError, SerializationError
from .pipeline import ColumnSpec, FeaturePipeline
from .transformers import (
    _NUMERIC_FILL_TAG,
    OneHotEncoder,
    SimpleImputer,
    StandardScaler,
)

FORMAT = "feature-pipeline"
VERSION = 1
_CHECKSUM_KEY = "checksum"

_STEP_CLASSES = {
    "impute": SimpleImputer,
    "standard_scale": StandardScaler,
    "one_hot_encode": OneHotEncoder,
}


def _spec_to_dict(spec: ColumnSpec) -> dict:
    fill = spec.fill_value
    if isinstance(fill, float) and fill != fill:  # NaN check without warnings
        fill = _NUMERIC_FILL_TAG
    return {
        "name": spec.name,
        "dtype": spec.dtype,
        "impute_strategy": spec.impute_strategy,
        "fill_value": fill,
        "scale": spec.scale,
        "handle_unknown": spec.handle_unknown,
    }


def _spec_from_dict(payload: dict) -> ColumnSpec:
    fill = payload["fill_value"]
    if fill == _NUMERIC_FILL_TAG:
        fill = float("nan")
    return ColumnSpec(
        name=payload["name"],
        dtype=payload["dtype"],
        impute_strategy=payload["impute_strategy"],
        fill_value=fill,
        scale=payload["scale"],
        handle_unknown=payload["handle_unknown"],
    )


def _canonical(payload: dict) -> bytes:
    """Canonical byte form: sorted keys, separators fixed, no ASCII escaping."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def pipeline_to_dict(pipeline: FeaturePipeline) -> dict:
    """Build the checksum-signed JSON-safe dictionary for a fitted pipeline."""
    if not pipeline.is_fitted:
        raise NotFittedError("Cannot serialize an unfitted pipeline.")

    schema = [_spec_to_dict(spec) for spec in pipeline.columns]
    columns: list[dict] = []
    for spec in pipeline.columns:
        _, imputer, second = pipeline.transformers_[spec.name]
        steps = [imputer.to_dict()]
        if second is not None:
            steps.append(second.to_dict())
        columns.append({"name": spec.name, "steps": steps})

    body: dict[str, Any] = {
        "format": FORMAT,
        "version": VERSION,
        "schema": schema,
        "columns": columns,
        "feature_names_out": list(pipeline.feature_names_out),
    }
    # Sign everything except the checksum itself.
    body[_CHECKSUM_KEY] = hashlib.sha256(_canonical(body)).hexdigest()
    return body


def pipeline_from_dict(doc: dict) -> FeaturePipeline:
    """Reconstruct a fitted pipeline, verifying format and checksum."""
    required = {"format", "version", "schema", "columns",
                "feature_names_out", _CHECKSUM_KEY}
    missing = required - set(doc)
    if missing:
        raise SerializationError(f"Artifact missing fields: {sorted(missing)}.")
    if doc["format"] != FORMAT or doc["version"] != VERSION:
        raise SerializationError(
            f"Unsupported artifact format/version: "
            f"{doc['format']!r}/v{doc['version']}."
        )

    stored = doc[_CHECKSUM_KEY]
    unsigned = {k: v for k, v in doc.items() if k != _CHECKSUM_KEY}
    if hashlib.sha256(_canonical(unsigned)).hexdigest() != stored:
        raise SerializationError(
            "Checksum mismatch: artifact was corrupted or tampered with."
        )

    try:
        specs = [_spec_from_dict(item) for item in doc["schema"]]
        pipeline = FeaturePipeline(specs)

        fitted: dict[str, Any] = {}
        for column_block in doc["columns"]:
            name = column_block["name"]
            spec = next(s for s in specs if s.name == name)
            steps = list(column_block["steps"])
            imputer = SimpleImputer.from_dict(steps[0])
            second = None
            if len(steps) > 1:
                kind = steps[1]["step"]
                second = _STEP_CLASSES[kind].from_dict(steps[1])
            fitted[name] = (spec, imputer, second)

        pipeline.transformers_ = fitted
        pipeline.feature_names_out = list(doc["feature_names_out"])
        pipeline._fitted = True
    except SerializationError:
        raise
    except (KeyError, TypeError, ValueError, StopIteration) as exc:
        raise SerializationError(f"Malformed artifact: {exc}") from exc

    return pipeline


PathLike = Union[str, Path]


def save_pipeline(pipeline: FeaturePipeline, path: PathLike) -> Path:
    """Serialize a fitted pipeline to a checksum-signed JSON file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        doc = pipeline_to_dict(pipeline)
    except NotFittedError as exc:
        raise SerializationError(str(exc)) from exc
    target.write_text(
        json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return target


def load_pipeline(path: PathLike) -> FeaturePipeline:
    """Load and verify a pipeline from a JSON artifact."""
    source = Path(path)
    if not source.exists():
        raise SerializationError(f"No artifact found at {source}.")
    try:
        doc = json.loads(source.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SerializationError(f"Artifact is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise SerializationError("Artifact root must be a JSON object.")
    return pipeline_from_dict(doc)
