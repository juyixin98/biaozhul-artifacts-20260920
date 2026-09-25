"""Column schema: ordered column definitions with dtype and impute strategy."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

NUMERIC = "numeric"
CATEGORICAL = "categorical"

NUMERIC_STRATEGIES = frozenset({"mean", "median", "constant"})
CATEGORICAL_STRATEGIES = frozenset({"mode", "constant"})


class SchemaError(ValueError):
    """Raised when a schema definition or input data violates the schema."""


@dataclass(frozen=True)
class ColumnSpec:
    """One input column.

    impute:
        numeric     -> "mean" | "median" | "constant" (default "mean")
        categorical -> "mode" | "constant" (default "mode")
    fill_value is required (numeric/categorical) when impute == "constant".
    """

    name: str
    dtype: str
    impute: str | None = None
    fill_value: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise SchemaError("column name must be a non-empty string")
        if self.dtype == NUMERIC:
            strategy = self.impute or "mean"
            allowed = NUMERIC_STRATEGIES
        elif self.dtype == CATEGORICAL:
            strategy = self.impute or "mode"
            allowed = CATEGORICAL_STRATEGIES
        else:
            raise SchemaError(
                f"column {self.name!r}: dtype must be "
                f"{NUMERIC!r} or {CATEGORICAL!r}, got {self.dtype!r}"
            )
        if strategy not in allowed:
            raise SchemaError(
                f"column {self.name!r}: impute strategy {strategy!r} "
                f"not in {sorted(allowed)} for {self.dtype} columns"
            )
        if strategy == "constant" and self.fill_value is None:
            raise SchemaError(
                f"column {self.name!r}: constant imputation requires fill_value"
            )
        if self.dtype == NUMERIC and self.fill_value is not None:
            if isinstance(self.fill_value, bool) or not isinstance(
                self.fill_value, (int, float)
            ):
                raise SchemaError(
                    f"column {self.name!r}: numeric fill_value must be a number"
                )
        object.__setattr__(self, "impute", strategy)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "dtype": self.dtype,
            "impute": self.impute,
            "fill_value": self.fill_value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ColumnSpec":
        try:
            return cls(
                name=data["name"],
                dtype=data["dtype"],
                impute=data.get("impute"),
                fill_value=data.get("fill_value"),
            )
        except KeyError as exc:
            raise SchemaError(f"column spec missing key: {exc}") from exc


@dataclass(frozen=True)
class Schema:
    """Ordered collection of column specs, optionally with a target column."""

    columns: tuple[ColumnSpec, ...] = field(default_factory=tuple)
    target: str | None = None

    def __post_init__(self) -> None:
        if not self.columns:
            raise SchemaError("schema must declare at least one column")
        names = [c.name for c in self.columns]
        if len(set(names)) != len(names):
            raise SchemaError(f"duplicate column names in schema: {names}")
        if self.target is not None:
            if not isinstance(self.target, str) or not self.target:
                raise SchemaError("target must be a non-empty string or null")
            if self.target in names:
                raise SchemaError(
                    f"target {self.target!r} must not also be a feature column"
                )

    @property
    def feature_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def get(self, name: str) -> ColumnSpec:
        for col in self.columns:
            if col.name == name:
                return col
        raise SchemaError(f"unknown column {name!r}")

    def validate_rows(self, rows: list[dict[str, Any]]) -> None:
        """Check rows are dicts and every schema column is present in each."""
        if not isinstance(rows, list) or not rows:
            raise SchemaError("rows must be a non-empty list of objects")
        required = set(self.feature_names)
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise SchemaError(f"row {index} must be an object, got {type(row)}")
            missing = required - row.keys()
            if missing:
                raise SchemaError(
                    f"row {index} is missing columns: {sorted(missing)}"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "columns": [c.to_dict() for c in self.columns],
            "target": self.target,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Schema":
        try:
            columns = tuple(
                ColumnSpec.from_dict(c) for c in data["columns"]
            )
        except KeyError as exc:
            raise SchemaError(f"schema missing key: {exc}") from exc
        return cls(columns=columns, target=data.get("target"))
