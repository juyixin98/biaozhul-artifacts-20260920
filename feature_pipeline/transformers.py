"""Individual, fit/transform-split preprocessing steps built on NumPy only.

Contract shared by every transformer:
    fit(column) -> self           : learns immutable state from TRAIN data
    transform(column) -> result   : applies that frozen state; never mutates it
    fit_transform(column)         : convenience for training
    fitted_ / is_fitted()         : state guard
    to_dict() / from_dict()       : deterministic JSON-safe (de)serialization

Transformers never mutate their input array.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from .exceptions import FittingError, NotFittedError

UNKNOWN_TOKEN = "__unknown__"
_NUMERIC_FILL_TAG = "__numeric_nan__"


def is_missing(value: Any) -> bool:
    """Unified missing-value test for object arrays (None / NaN)."""
    if value is None:
        return True
    try:
        return bool(isinstance(value, float) and np.isnan(value))
    except (TypeError, ValueError):
        return False


def missing_mask(column: np.ndarray) -> np.ndarray:
    """Boolean mask of missing entries for object or float arrays."""
    if column.dtype.kind in "fc":
        return np.isnan(column.astype(float))
    vectorized = np.vectorize(is_missing, otypes=[bool])
    return vectorized(column)


class _BaseTransformer:
    def __init__(self) -> None:
        self._fitted = False

    def is_fitted(self) -> bool:
        return self._fitted

    def _ensure_fitted(self) -> None:
        if not self._fitted:
            raise NotFittedError(
                f"{type(self).__name__} is not fitted; call fit() on training data first."
            )

    def fit(self, column: np.ndarray) -> "_BaseTransformer":  # pragma: no cover
        raise NotImplementedError

    def transform(self, column: np.ndarray) -> np.ndarray:  # pragma: no cover
        raise NotImplementedError

    def fit_transform(self, column: np.ndarray) -> np.ndarray:
        return self.fit(column).transform(column)

    def to_dict(self) -> dict:  # pragma: no cover
        raise NotImplementedError

    @classmethod
    def from_dict(cls, payload: dict) -> "_BaseTransformer":  # pragma: no cover
        raise NotImplementedError


class SimpleImputer(_BaseTransformer):
    """Fills missing values using a statistic learned on training data.

    Numeric strategies: mean, median, constant.
    Categorical strategies: most_frequent (deterministic tie-break), constant.
    """

    def __init__(self, strategy: str = "mean", fill_value: Any = None) -> None:
        super().__init__()
        valid = {"mean", "median", "most_frequent", "constant"}
        if strategy not in valid:
            raise FittingError(
                f"Unknown impute strategy {strategy!r}; expected one of {sorted(valid)}."
            )
        if strategy == "constant" and fill_value is None:
            raise FittingError("strategy='constant' requires an explicit fill_value.")
        self.strategy = strategy
        self.fill_value = fill_value
        self.fill_value_: Any = None

    def fit(self, column: np.ndarray) -> "SimpleImputer":
        mask = missing_mask(column)
        observed = column[~mask]
        if self.strategy == "constant":
            learned = self.fill_value
        elif observed.size == 0:
            raise FittingError(
                "Cannot fit imputer: column is entirely missing, so no statistic "
                "can be learned from training data."
            )
        elif self.strategy in {"mean", "median"}:
            values = observed.astype(float)
            learned = float(values.mean()) if self.strategy == "mean" \
                else float(np.median(values))
        else:  # most_frequent, deterministic: highest frequency then lexicographic
            labels, counts = np.unique(observed.astype(str), return_counts=True)
            top_count = counts.max()
            learned = str(sorted(labels[counts == top_count])[0])

        self.fill_value_ = learned
        self._fitted = True
        return self

    def transform(self, column: np.ndarray) -> np.ndarray:
        self._ensure_fitted()
        result = np.array(column, copy=True)
        mask = missing_mask(result)
        if mask.any():
            result[mask] = self.fill_value_
        return result

    def to_dict(self) -> dict:
        self._ensure_fitted()
        fill = self.fill_value_
        # JSON has no NaN; tag learned numeric fills so NaN itself round-trips.
        if isinstance(fill, float) and np.isnan(fill):
            params = {"strategy": self.strategy, "fill_value": _NUMERIC_FILL_TAG}
        else:
            params = {"strategy": self.strategy, "fill_value": fill}
        return {"step": "impute", "params": params}

    @classmethod
    def from_dict(cls, payload: dict) -> "SimpleImputer":
        params = payload["params"]
        raw = params["fill_value"]
        fill = np.nan if raw == _NUMERIC_FILL_TAG else raw
        obj = cls(strategy=params["strategy"],
                  fill_value=fill if params["strategy"] == "constant" else None)
        obj.fill_value_ = fill
        obj._fitted = True
        return obj


class StandardScaler(_BaseTransformer):
    """z = (x - mean) / scale with population std and a zero-variance safeguard."""

    def __init__(self) -> None:
        super().__init__()
        self.mean_ = 0.0
        self.scale_ = 1.0
        self.zero_variance_ = False

    def fit(self, column: np.ndarray) -> "StandardScaler":
        values = column.astype(float)
        self.mean_ = float(values.mean())
        std = float(values.std())  # population standard deviation (ddof=0)
        self.zero_variance_ = std == 0.0
        # Fall back to scale 1 on constant columns: output becomes all zeros
        # instead of dividing by zero.
        self.scale_ = 1.0 if self.zero_variance_ else std
        self._fitted = True
        return self

    def transform(self, column: np.ndarray) -> np.ndarray:
        self._ensure_fitted()
        return (column.astype(float) - self.mean_) / self.scale_

    def to_dict(self) -> dict:
        self._ensure_fitted()
        return {
            "step": "standard_scale",
            "params": {
                "mean": self.mean_,
                "scale": self.scale_,
                "zero_variance": self.zero_variance_,
            },
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "StandardScaler":
        params = payload["params"]
        obj = cls()
        obj.mean_ = float(params["mean"])
        obj.scale_ = float(params["scale"])
        obj.zero_variance_ = bool(params["zero_variance"])
        obj._fitted = True
        return obj


class OneHotEncoder(_BaseTransformer):
    """Sorted-category one-hot encoder.

    handle_unknown="ignore":     unseen values -> all-zero vector
    handle_unknown="indicator":  unseen values -> all-zero + trailing 1 flag
    """

    def __init__(self, handle_unknown: str = "indicator") -> None:
        super().__init__()
        if handle_unknown not in {"ignore", "indicator"}:
            raise FittingError(
                "handle_unknown must be 'ignore' or 'indicator', "
                f"got {handle_unknown!r}."
            )
        self.handle_unknown = handle_unknown
        self.categories_: list[str] = []

    @property
    def n_columns_out_(self) -> int:
        self._ensure_fitted()
        extra = 1 if self.handle_unknown == "indicator" else 0
        return len(self.categories_) + extra

    def fit(self, column: np.ndarray) -> "OneHotEncoder":
        mask = missing_mask(column)
        observed = column[~mask].astype(str)
        if observed.size == 0:
            raise FittingError(
                "Cannot fit one-hot encoder: categorical column is entirely missing."
            )
        self.categories_ = sorted(np.unique(observed).tolist())
        self._fitted = True
        return self

    def transform(self, column: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Returns (one-hot matrix, boolean unknown-per-row mask)."""
        self._ensure_fitted()
        n = column.shape[0]
        width = self.n_columns_out_
        matrix = np.zeros((n, width), dtype=float)
        unknown = np.zeros(n, dtype=bool)
        index = {cat: i for i, cat in enumerate(self.categories_)}

        for row, raw in enumerate(column):
            if is_missing(raw):
                continue  # imputation runs upstream; leave an all-zero row
            label = str(raw)
            pos = index.get(label)
            if pos is None:
                unknown[row] = True
                if self.handle_unknown == "indicator":
                    matrix[row, len(self.categories_)] = 1.0
            else:
                matrix[row, pos] = 1.0
        return matrix, unknown

    def fit_transform(self, column: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return self.fit(column).transform(column)

    def to_dict(self) -> dict:
        self._ensure_fitted()
        return {
            "step": "one_hot_encode",
            "params": {
                "categories": list(self.categories_),
                "handle_unknown": self.handle_unknown,
            },
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "OneHotEncoder":
        params = payload["params"]
        obj = cls(handle_unknown=params["handle_unknown"])
        obj.categories_ = [str(c) for c in params["categories"]]
        obj._fitted = True
        return obj
