"""Serializable model artifact: ordered schema + fitted pipeline + model.

The artifact is the single unit persisted to disk. JSON is used so the
column order, categorical vocabulary and fitted parameters are human
inspectable. Feature names are stored alongside weights to document the
matrix layout and detect mismatches on load.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from .errors import NotFittedError
from .model import LinearRegressionModel
from .pipeline import Pipeline
from .schema import Schema

ARTIFACT_VERSION = 1
ARTIFACT_KIND = "feature_pipeline_artifact"


class ArtifactError(ValueError):
    """Raised when an artifact file is malformed or version-incompatible."""


class ModelArtifact:
    """Bundle of schema, fitted pipeline and trained regression model."""

    def __init__(
        self,
        schema: Schema,
        pipeline: Pipeline,
        model: LinearRegressionModel,
        feature_names: list[str] | None = None,
    ):
        self.schema = schema
        self.pipeline = pipeline
        self.model = model
        self.feature_names = feature_names

    @classmethod
    def fit(
        cls,
        schema: Schema,
        rows: list[dict[str, Any]],
        targets: list[float] | np.ndarray,
    ) -> "ModelArtifact":
        """Fit pipeline on training rows, then the model on the matrix.

        ``schema.columns`` are feature columns; ``schema.target`` records
        the target column name for documentation. Target values are passed
        separately, guaranteeing they never enter the fit of transformers.
        """
        if schema.target is None:
            raise ValueError(
                "schema.target must name the target column for artifact.fit"
            )
        pipeline = Pipeline(schema)
        x_train = pipeline.fit_transform(rows)
        y_train = np.asarray(targets, dtype=np.float64)
        model = LinearRegressionModel().fit(x_train, y_train)
        return cls(schema, pipeline, model,
                   list(pipeline.feature_names_out))

    def predict_rows(self, rows: list[dict[str, Any]]) -> np.ndarray:
        """Transform inference rows (read-only) and run the model."""
        if not self.pipeline.is_fitted or not self.model.is_fitted:
            raise NotFittedError("artifact is not fitted")
        x = self.pipeline.transform(rows)
        return self.model.predict(x)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": ARTIFACT_KIND,
            "version": ARTIFACT_VERSION,
            "schema": self.schema.to_dict(),
            "feature_names": list(self.feature_names or []),
            "pipeline": self.pipeline.to_dict(),
            "model": self.model.to_dict(),
        }

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2, sort_keys=False,
                      ensure_ascii=False)
            handle.write("\n")
        tmp.replace(path)
        return path

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelArtifact":
        if data.get("kind") != ARTIFACT_KIND:
            raise ArtifactError(
                f"not an artifact: kind={data.get('kind')!r}"
            )
        if data.get("version") != ARTIFACT_VERSION:
            raise ArtifactError(
                f"unsupported artifact version: {data.get('version')!r}, "
                f"expected {ARTIFACT_VERSION}"
            )
        schema = Schema.from_dict(data["schema"])
        pipeline = Pipeline.from_dict(data["pipeline"])
        model = LinearRegressionModel.from_dict(data["model"])
        artifact = cls(
            schema, pipeline, model,
            feature_names=[str(n) for n in data.get("feature_names", [])],
        )
        artifact._validate_consistency()
        return artifact

    @classmethod
    def load(cls, path: str | Path) -> "ModelArtifact":
        path = Path(path)
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return cls.from_dict(data)

    def _validate_consistency(self) -> None:
        """Fail fast if schema, pipeline and model disagree on dimensions."""
        if self.pipeline.schema.feature_names != self.schema.feature_names:
            raise ArtifactError("pipeline schema does not match artifact schema")
        if self.feature_names and self.pipeline.is_fitted:
            live_names = self.pipeline.feature_names_out
            if live_names != self.feature_names:
                raise ArtifactError(
                    "stored feature names do not match pipeline output: "
                    f"stored={self.feature_names} actual={live_names}"
                )
        if self.model.is_fitted and self.pipeline.is_fitted:
            n_encoded = len(self.feature_names) if self.feature_names else None
            if n_encoded is not None and self.model.n_features_ != n_encoded:
                raise ArtifactError(
                    f"model expects {self.model.n_features_} features but "
                    f"pipeline produces {n_encoded}"
                )
