"""Ordered, schema-validated feature pipeline.

Per numeric column:     SimpleImputer -> optional StandardScaler
Per categorical column: SimpleImputer -> OneHotEncoder

State is learned exclusively in fit() on TRAIN data. transform() applies that
frozen state read-only: it never re-fits, never mutates inputs, and always
emits columns in the order declared by the schema (regardless of input order).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

from .exceptions import FittingError, NotFittedError, SchemaError
from .transformers import (
    UNKNOWN_TOKEN,
    OneHotEncoder,
    SimpleImputer,
    StandardScaler,
    missing_mask,
)


@dataclass(frozen=True)
class ColumnSpec:
    """Declarative description of one input column."""

    name: str
    dtype: str  # "numeric" or "categorical"
    impute_strategy: str = "mean"
    fill_value: Any = None
    scale: bool = True  # numeric only
    handle_unknown: str = "indicator"  # categorical only

    def __post_init__(self) -> None:
        if self.dtype not in {"numeric", "categorical"}:
            raise SchemaError(
                f"Column {self.name!r}: dtype must be 'numeric' or 'categorical', "
                f"got {self.dtype!r}."
            )
        if self.impute_strategy not in {"mean", "median", "most_frequent", "constant"}:
            raise SchemaError(
                f"Column {self.name!r}: unknown impute_strategy "
                f"{self.impute_strategy!r}."
            )
        if self.dtype == "numeric" and self.impute_strategy == "most_frequent":
            raise SchemaError(
                f"Column {self.name!r}: 'most_frequent' is for categorical columns."
            )
        if self.dtype == "categorical" and self.impute_strategy in {"mean", "median"}:
            raise SchemaError(
                f"Column {self.name!r}: {self.impute_strategy!r} is for numeric columns."
            )


@dataclass
class TransformResult:
    """Output matrix plus per-categorical-column unknown-category masks."""

    X: np.ndarray
    feature_names_out: list[str]
    unknown_categories: dict[str, np.ndarray] = field(default_factory=dict)


class FeaturePipeline:
    """Fit-once, transform-many pipeline enforcing a fixed column schema."""

    def __init__(self, columns: list[ColumnSpec], strict: bool = True) -> None:
        if not columns:
            raise SchemaError("A pipeline requires at least one ColumnSpec.")
        names = [c.name for c in columns]
        if len(set(names)) != len(names):
            raise SchemaError(f"Duplicate column names in schema: {names}.")
        self.columns = list(columns)
        self.strict = strict
        self._fitted = False
        # name -> (ColumnSpec, imputer, optional scaler/encoder)
        self.transformers_: dict[str, tuple[ColumnSpec, SimpleImputer, Any]] = {}
        self.feature_names_out: list[str] = []

    # ------------------------------------------------------------------ fit #
    def fit(self, data: dict[str, np.ndarray]) -> "FeaturePipeline":
        ordered = self._validate_schema(data, allow_fit=True)
        names_out: list[str] = []
        fitted: dict[str, tuple[ColumnSpec, SimpleImputer, Any]] = {}

        for spec in self.columns:
            column = ordered[spec.name]
            imputer = SimpleImputer(
                strategy=spec.impute_strategy, fill_value=spec.fill_value
            )
            imputed = imputer.fit_transform(column)

            if spec.dtype == "numeric":
                second: Any = StandardScaler().fit(imputed) if spec.scale else None
                names_out.append(spec.name)
            else:
                second = OneHotEncoder(handle_unknown=spec.handle_unknown)
                second.fit(imputed)
                names_out.extend(f"{spec.name}={cat}" for cat in second.categories_)
                if spec.handle_unknown == "indicator":
                    names_out.append(f"{spec.name}={UNKNOWN_TOKEN}")
            fitted[spec.name] = (spec, imputer, second)

        self.transformers_ = fitted
        self.feature_names_out = names_out
        self._fitted = True
        return self

    def fit_transform(self, data: dict[str, np.ndarray]) -> TransformResult:
        return self.fit(data)._apply(data)

    # -------------------------------------------------------------- transform #
    def transform(self, data: dict[str, np.ndarray]) -> TransformResult:
        if not self._fitted:
            raise NotFittedError(
                "Pipeline is not fitted; call fit() on training data before transform()."
            )
        ordered = self._validate_schema(data, allow_fit=False)
        return self._apply(ordered)

    def _apply(self, ordered: dict[str, np.ndarray]) -> TransformResult:
        """Apply frozen transformers. Inputs are copied by each transformer."""
        blocks: list[np.ndarray] = []
        unknown_flags: dict[str, np.ndarray] = {}

        for spec in self.columns:
            _, imputer, second = self.transformers_[spec.name]
            imputed = imputer.transform(ordered[spec.name])
            if spec.dtype == "numeric":
                block = second.transform(imputed) if second is not None \
                    else imputed.astype(float)
                blocks.append(np.asarray(block, dtype=float).reshape(-1, 1))
            else:
                one_hot, unknown = second.transform(imputed)
                blocks.append(one_hot)
                unknown_flags[spec.name] = unknown

        return TransformResult(
            X=np.hstack(blocks),
            feature_names_out=list(self.feature_names_out),
            unknown_categories=unknown_flags,
        )

    # ----------------------------------------------------------------- schema #
    def _validate_schema(
        self, data: dict[str, np.ndarray], allow_fit: bool
    ) -> dict[str, np.ndarray]:
        if not isinstance(data, dict):
            raise SchemaError("Input must be a dict mapping column name -> np.ndarray.")

        expected = [c.name for c in self.columns]
        actual = list(data.keys())

        missing = [n for n in expected if n not in data]
        if missing:
            raise SchemaError(f"Missing required column(s): {missing}.")

        unexpected = [n for n in actual if n not in expected]
        if self.strict and unexpected:
            raise SchemaError(
                f"Unexpected column(s) {unexpected}; schema is {expected}."
            )

        n_rows: Optional[int] = None
        ordered: dict[str, np.ndarray] = {}
        for name in expected:
            column = np.asarray(data[name])
            if column.ndim != 1:
                raise SchemaError(f"Column {name!r} must be 1-dimensional.")
            if n_rows is None:
                n_rows = column.shape[0]
            elif column.shape[0] != n_rows:
                raise SchemaError(
                    f"Column {name!r} has {column.shape[0]} rows; expected {n_rows} "
                    "to match the other columns."
                )
            ordered[name] = column

        if allow_fit:
            self._validate_fit_content(ordered)
        return ordered

    def _validate_fit_content(self, ordered: dict[str, np.ndarray]) -> None:
        for spec in self.columns:
            column = ordered[spec.name]
            if spec.dtype == "numeric":
                try:
                    column.astype(float)
                except (TypeError, ValueError) as exc:
                    raise FittingError(
                        f"Numeric column {spec.name!r} contains non-numeric data."
                    ) from exc
            if missing_mask(column).all():
                raise FittingError(
                    f"Column {spec.name!r} is entirely missing during fit; "
                    "no state can be learned."
                )

    # ------------------------------------------------------------- introspect #
    @property
    def is_fitted(self) -> bool:
        return self._fitted
