"""Column-level transformers: imputation, scaling, one-hot encoding.

Every transformer follows the same lifecycle:
    fit(values)   -> self, computes state from TRAINING data only
    transform(values) -> np.ndarray, read-only use of fitted state
    to_dict()/from_dict() for serialization

Transformers never mutate fitted state inside transform().
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import numpy as np

from .errors import NotFittedError
from .schema import CATEGORICAL, NUMERIC


def _require_fitted(obj: Any) -> None:
    if not obj.is_fitted:
        raise NotFittedError(
            f"{type(obj).__name__} is not fitted; call fit() first"
        )


class BaseTransformer(ABC):
    """Common fit/transform interface."""

    @property
    def is_fitted(self) -> bool:
        return getattr(self, "_fitted", False)

    @abstractmethod
    def fit(self, values: np.ndarray) -> "BaseTransformer":
        """Compute fitted state from one column of training data."""

    @abstractmethod
    def transform(self, values: np.ndarray) -> np.ndarray:
        """Apply the transformation using fitted state only."""

    @abstractmethod
    def to_dict(self) -> dict[str, Any]:
        """Serialize fitted parameters."""

    def fit_transform(self, values: np.ndarray) -> np.ndarray:
        return self.fit(values).transform(values)


def to_numeric(values: np.ndarray) -> np.ndarray:
    """Coerce a 1-D array to float64; None/NaN/blank become NaN.

    Booleans and non-numeric strings raise TypeError so that genuinely
    malformed numeric input is never silently imputed.
    """
    out = np.empty(len(values), dtype=np.float64)
    for i, value in enumerate(np.asarray(values, dtype=object)):
        if value is None or (isinstance(value, float) and np.isnan(value)):
            out[i] = np.nan
        elif isinstance(value, str) and value.strip() == "":
            out[i] = np.nan
        elif isinstance(value, bool):
            raise TypeError(
                f"boolean is not a valid numeric value at position {i}"
            )
        elif isinstance(value, (int, float, np.integer, np.floating)):
            out[i] = float(value)
        else:
            raise TypeError(
                f"cannot interpret {value!r} ({type(value).__name__}) "
                f"as numeric at position {i}"
            )
    return out


def is_missing_categorical(value: Any) -> bool:
    return value is None or (isinstance(value, float) and np.isnan(value))


class NumericImputer(BaseTransformer):
    """Fill missing numeric values with mean / median / constant."""

    def __init__(self, strategy: str = "mean", fill_value: float | None = None):
        if strategy not in ("mean", "median", "constant"):
            raise ValueError(f"unknown numeric impute strategy: {strategy!r}")
        if strategy == "constant" and fill_value is None:
            raise ValueError("constant strategy requires fill_value")
        self.strategy = strategy
        self.fill_value = (
            float(fill_value) if fill_value is not None else None
        )
        self.statistic_: float | None = None
        self.n_seen_: int = 0
        self._fitted = False

    def fit(self, values: np.ndarray) -> "NumericImputer":
        numeric = to_numeric(values)
        observed = numeric[~np.isnan(numeric)]
        self.n_seen_ = int(numeric.size)
        if self.strategy == "mean":
            if observed.size == 0:
                raise ValueError(
                    "cannot fit mean imputer: column has no observed values"
                )
            self.statistic_ = float(observed.mean())
        elif self.strategy == "median":
            if observed.size == 0:
                raise ValueError(
                    "cannot fit median imputer: column has no observed values"
                )
            self.statistic_ = float(np.median(observed))
        else:  # constant
            self.statistic_ = self.fill_value
        self._fitted = True
        return self

    def transform(self, values: np.ndarray) -> np.ndarray:
        _require_fitted(self)
        numeric = to_numeric(values)
        return np.where(np.isnan(numeric), self.statistic_, numeric)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "numeric_imputer",
            "strategy": self.strategy,
            "fill_value": self.fill_value,
            "statistic": self.statistic_,
            "n_seen": self.n_seen_,
            "fitted": self._fitted,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NumericImputer":
        obj = cls(strategy=data["strategy"], fill_value=data.get("fill_value"))
        if data.get("fitted"):
            if data.get("statistic") is None:
                raise ValueError("fitted imputer serialized without statistic")
            obj.statistic_ = float(data["statistic"])
            obj.n_seen_ = int(data.get("n_seen", 0))
            obj._fitted = True
        return obj


class CategoricalImputer(BaseTransformer):
    """Fill missing categorical values with the training mode or a constant."""

    def __init__(self, strategy: str = "mode", fill_value: str | None = None):
        if strategy not in ("mode", "constant"):
            raise ValueError(
                f"unknown categorical impute strategy: {strategy!r}"
            )
        if strategy == "constant" and fill_value is None:
            raise ValueError("constant strategy requires fill_value")
        self.strategy = strategy
        self.fill_value = fill_value
        self.statistic_: str | None = None
        self.n_seen_ = 0
        self._fitted = False

    def fit(self, values: np.ndarray) -> "CategoricalImputer":
        raw = np.asarray(values, dtype=object)
        observed = [
            self._as_category(v, pos)
            for pos, v in enumerate(raw)
            if not is_missing_categorical(v)
        ]
        self.n_seen_ = int(raw.size)
        if self.strategy == "mode":
            if not observed:
                raise ValueError(
                    "cannot fit mode imputer: column has no observed values"
                )
            self.statistic_ = self._mode(observed)
        else:  # constant
            self.statistic_ = str(self.fill_value)
        self._fitted = True
        return self

    def transform(self, values: np.ndarray) -> np.ndarray:
        _require_fitted(self)
        raw = np.asarray(values, dtype=object)
        out = np.empty(raw.size, dtype=object)
        for i, value in enumerate(raw):
            if is_missing_categorical(value):
                out[i] = self.statistic_
            else:
                out[i] = self._as_category(value, i)
        return out

    @staticmethod
    def _as_category(value: Any, pos: int) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, (int, np.integer)) and not isinstance(value, bool):
            return str(int(value))
        raise TypeError(
            f"categorical value at position {pos} must be a string, "
            f"got {value!r} ({type(value).__name__})"
        )

    @staticmethod
    def _mode(observed: list[str]) -> str:
        """Most frequent category; ties broken by lexicographic order."""
        counts: dict[str, int] = {}
        for value in observed:
            counts[value] = counts.get(value, 0) + 1
        top_count = max(counts.values())
        return sorted(v for v, count in counts.items() if count == top_count)[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "categorical_imputer",
            "strategy": self.strategy,
            "fill_value": self.fill_value,
            "statistic": self.statistic_,
            "n_seen": self.n_seen_,
            "fitted": self._fitted,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CategoricalImputer":
        obj = cls(strategy=data["strategy"], fill_value=data.get("fill_value"))
        if data.get("fitted"):
            if data.get("statistic") is None:
                raise ValueError("fitted imputer serialized without statistic")
            obj.statistic_ = str(data["statistic"])
            obj.n_seen_ = int(data.get("n_seen", 0))
            obj._fitted = True
        return obj


class StandardScaler(BaseTransformer):
    """Zero-mean, unit-variance scaling.

    Zero-variance columns keep their mean and store scale = 1.0, so
    transformed values are all 0.0 instead of raising ZeroDivisionError.
    """

    EPSILON = 1e-12

    def __init__(self) -> None:
        self.mean_: float | None = None
        self.std_: float | None = None
        self.scale_: float | None = None
        self.zero_variance_: bool = False
        self._fitted = False

    def fit(self, values: np.ndarray) -> "StandardScaler":
        numeric = np.asarray(values, dtype=np.float64)
        if np.isnan(numeric).any():
            raise ValueError(
                "StandardScaler.fit received NaN; imputation must run first"
            )
        self.mean_ = float(numeric.mean())
        self.std_ = float(numeric.std(ddof=0))
        self.zero_variance_ = self.std_ <= self.EPSILON
        self.scale_ = 1.0 if self.zero_variance_ else self.std_
        self._fitted = True
        return self

    def transform(self, values: np.ndarray) -> np.ndarray:
        _require_fitted(self)
        numeric = np.asarray(values, dtype=np.float64)
        return (numeric - self.mean_) / self.scale_

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "standard_scaler",
            "mean": self.mean_,
            "std": self.std_,
            "scale": self.scale_,
            "zero_variance": self.zero_variance_,
            "fitted": self._fitted,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StandardScaler":
        obj = cls()
        if data.get("fitted"):
            obj.mean_ = float(data["mean"])
            obj.std_ = float(data["std"])
            obj.scale_ = float(data["scale"])
            obj.zero_variance_ = bool(data.get("zero_variance"))
            obj._fitted = True
        return obj


class OneHotEncoder(BaseTransformer):
    """One-hot encoding with an all-zero vector for unknown categories.

    Categories are learned from training data only and stored sorted for
    deterministic output. Unseen categories at inference time encode to a
    vector of zeros (handle_unknown="zero").
    """

    def __init__(self, column_name: str, categories: list[str] | None = None):
        self.column_name = column_name
        self.categories_: list[str] | None = None
        self._index: dict[str, int] = {}
        if categories is not None:
            # Used when building an encoder outside a fit() call (tests).
            self._set_categories(list(categories))
            self._fitted = True
        else:
            self._fitted = False

    def _set_categories(self, categories: list[str]) -> None:
        self.categories_ = sorted(categories)
        self._index = {value: i for i, value in enumerate(self.categories_)}

    def fit(self, values: np.ndarray) -> "OneHotEncoder":
        raw = np.asarray(values, dtype=object)
        categories: set[str] = set()
        for pos, value in enumerate(raw):
            if is_missing_categorical(value):
                raise ValueError(
                    f"OneHotEncoder.fit received missing value at position "
                    f"{pos}; categorical imputation must run first"
                )
            if not isinstance(value, str):
                raise TypeError(
                    f"OneHotEncoder expected strings after imputation, got "
                    f"{value!r} ({type(value).__name__}) at position {pos}"
                )
            categories.add(value)
        self._set_categories(list(categories))
        self._fitted = True
        return self

    def transform(self, values: np.ndarray) -> np.ndarray:
        _require_fitted(self)
        raw = np.asarray(values, dtype=object)
        n_categories = len(self.categories_)
        out = np.zeros((raw.size, n_categories), dtype=np.float64)
        for row, value in enumerate(raw):
            if not is_missing_categorical(value) and value in self._index:
                out[row, self._index[value]] = 1.0
        return out

    @property
    def feature_names(self) -> list[str]:
        _require_fitted(self)
        return [f"{self.column_name}={category}" for category in self.categories_]

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "one_hot_encoder",
            "column_name": self.column_name,
            "categories": self.categories_,
            "fitted": self._fitted,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OneHotEncoder":
        obj = cls(column_name=data["column_name"])
        if data.get("fitted"):
            if data.get("categories") is None:
                raise ValueError("fitted encoder serialized without categories")
            obj._set_categories([str(c) for c in data["categories"]])
            obj._fitted = True
        return obj


_TRANSFORMER_TYPES: dict[str, type[BaseTransformer]] = {
    "numeric_imputer": NumericImputer,
    "categorical_imputer": CategoricalImputer,
    "standard_scaler": StandardScaler,
    "one_hot_encoder": OneHotEncoder,
}


def transformer_from_dict(data: dict[str, Any]) -> BaseTransformer:
    """Deserialize any transformer by its ``type`` tag."""
    try:
        cls = _TRANSFORMER_TYPES[data["type"]]
    except KeyError as exc:
        raise ValueError(f"unknown transformer type: {data.get('type')!r}") from exc
    return cls.from_dict(data)


def build_column_steps(spec) -> list[BaseTransformer]:
    """Build the ordered, unfitted transformer list for one column."""
    if spec.dtype == NUMERIC:
        return [NumericImputer(strategy=spec.impute, fill_value=spec.fill_value),
                StandardScaler()]
    if spec.dtype == CATEGORICAL:
        return [CategoricalImputer(strategy=spec.impute,
                                   fill_value=spec.fill_value),
                OneHotEncoder(column_name=spec.name)]
    raise ValueError(f"unsupported dtype for column {spec.name!r}")
