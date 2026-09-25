"""Ordered per-column transformation pipeline.

The pipeline owns the schema and the ordered transformer list of every
column. Columns are always read *by name* from input rows, then emitted in
the fixed schema order, so inference rows whose keys arrive in a different
order (or with extra keys) produce the identical feature matrix.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .errors import NotFittedError
from .schema import Schema
from .transforms import (
    BaseTransformer,
    build_column_steps,
    transformer_from_dict,
)


class Pipeline:
    """Fit on training rows; transform new rows using frozen state."""

    def __init__(self, schema: Schema):
        self.schema = schema
        self._steps: dict[str, list[BaseTransformer]] = {
            spec.name: build_column_steps(spec) for spec in schema.columns
        }
        self._fitted = False

    @property
    def is_fitted(self) -> bool:
        return self._fitted

    def steps(self, column: str) -> list[BaseTransformer]:
        return self._steps[self.schema.get(column).name]

    def fit(self, rows: list[dict[str, Any]]) -> "Pipeline":
        """Fit every column's transformers on training data only."""
        self.schema.validate_rows(rows)
        for spec in self.schema.columns:
            values = self._column(rows, spec.name)
            for step in self._steps[spec.name]:
                values = step.fit_transform(values)
        self._fitted = True
        return self

    def transform(self, rows: list[dict[str, Any]]) -> np.ndarray:
        """Build the feature matrix; fitted state is never mutated."""
        if not self._fitted:
            raise NotFittedError("Pipeline is not fitted; call fit() first")
        self.schema.validate_rows(rows)
        blocks: list[np.ndarray] = []
        for spec in self.schema.columns:
            values = self._column(rows, spec.name)
            for step in self._steps[spec.name]:
                values = step.transform(values)
            block = np.asarray(values, dtype=np.float64)
            if block.ndim == 1:
                block = block.reshape(-1, 1)
            blocks.append(block)
        if not blocks:
            return np.empty((len(rows), 0), dtype=np.float64)
        return np.hstack(blocks)

    def fit_transform(self, rows: list[dict[str, Any]]) -> np.ndarray:
        return self.fit(rows).transform(rows)

    @staticmethod
    def _column(rows: list[dict[str, Any]], name: str) -> np.ndarray:
        return np.array([row[name] for row in rows], dtype=object)

    @property
    def feature_names_out(self) -> list[str]:
        """Output feature labels after expanding one-hot columns."""
        if not self._fitted:
            raise NotFittedError(
                "feature names are only known after fit (one-hot categories)"
            )
        names: list[str] = []
        for spec in self.schema.columns:
            tail = self._steps[spec.name][-1]
            if spec.dtype == "categorical":
                names.extend(tail.feature_names)
            else:
                names.append(spec.name)
        return names

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema.to_dict(),
            "fitted": self._fitted,
            "steps": [
                {
                    "column": spec.name,
                    "dtype": spec.dtype,
                    "transformers": [
                        step.to_dict() for step in self._steps[spec.name]
                    ],
                }
                for spec in self.schema.columns
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Pipeline":
        schema = Schema.from_dict(data["schema"])
        pipeline = cls(schema)
        if data.get("fitted"):
            by_name = {entry["column"]: entry for entry in data["steps"]}
            for spec in schema.columns:
                entry = by_name.get(spec.name)
                if entry is None or entry.get("dtype") != spec.dtype:
                    raise ValueError(
                        f"serialized steps missing/mismatched for column "
                        f"{spec.name!r}"
                    )
                steps = [
                    transformer_from_dict(step_data)
                    for step_data in entry["transformers"]
                ]
                if not steps or not all(s.is_fitted for s in steps):
                    raise ValueError(
                        f"column {spec.name!r}: serialized transformers "
                        f"must all be fitted"
                    )
                pipeline._steps[spec.name] = steps
            pipeline._fitted = True
        return pipeline
