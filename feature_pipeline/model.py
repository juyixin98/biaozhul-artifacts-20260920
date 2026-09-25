"""Simple least-squares linear regression over the transformed matrix.

The model is intentionally minimal (numpy.linalg.lstsq with an intercept
column) so the end-to-end fit/transform/predict mechanism can be verified
without external models.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .errors import NotFittedError

INTERCEPT_NAME = "__intercept__"


class LinearRegressionModel:
    """Ordinary least squares regression: y = X @ coef_ + intercept_."""

    def __init__(self) -> None:
        self.coef_: np.ndarray | None = None
        self.intercept_: float | None = None
        self.n_features_: int | None = None
        self.rank_: int | None = None
        self._fitted = False

    @property
    def is_fitted(self) -> bool:
        return self._fitted

    def fit(self, x: np.ndarray, y: np.ndarray) -> "LinearRegressionModel":
        matrix = np.asarray(x, dtype=np.float64)
        target = np.asarray(y, dtype=np.float64)
        if matrix.ndim != 2:
            raise ValueError(f"X must be 2-D, got shape {matrix.shape}")
        if target.ndim != 1 or target.shape[0] != matrix.shape[0]:
            raise ValueError(
                f"y must be 1-D with {matrix.shape[0]} rows, "
                f"got shape {target.shape}"
            )
        if not np.isfinite(matrix).all():
            raise ValueError("X contains NaN or inf; pipeline output must be finite")
        design = self._add_intercept(matrix)
        # rcond=None matches NumPy's recommended default (machine-precision cut).
        beta, _, rank, _ = np.linalg.lstsq(design, target, rcond=None)
        self.intercept_ = float(beta[0])
        self.coef_ = beta[1:].astype(np.float64)
        self.n_features_ = matrix.shape[1]
        self.rank_ = int(rank)
        self._fitted = True
        return self

    def predict(self, x: np.ndarray) -> np.ndarray:
        if not self._fitted:
            raise NotFittedError(
                "LinearRegressionModel is not fitted; call fit() first"
            )
        matrix = np.asarray(x, dtype=np.float64)
        if matrix.ndim != 2 or matrix.shape[1] != self.n_features_:
            raise ValueError(
                f"X must be 2-D with {self.n_features_} feature columns, "
                f"got shape {matrix.shape}"
            )
        return matrix @ self.coef_ + self.intercept_

    @staticmethod
    def _add_intercept(matrix: np.ndarray) -> np.ndarray:
        ones = np.ones((matrix.shape[0], 1), dtype=np.float64)
        return np.hstack([ones, matrix])

    def to_dict(self) -> dict[str, Any]:
        if not self._fitted:
            return {"fitted": False}
        return {
            "fitted": True,
            "coefficients": np.asarray(self.coef_).tolist(),
            "intercept": self.intercept_,
            "n_features": self.n_features_,
            "rank": self.rank_,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LinearRegressionModel":
        model = cls()
        if data.get("fitted"):
            model.coef_ = np.asarray(data["coefficients"], dtype=np.float64)
            model.intercept_ = float(data["intercept"])
            model.n_features_ = int(data["n_features"])
            model.rank_ = int(data["rank"])
            if model.coef_.shape[0] != model.n_features_:
                raise ValueError("serialized coefficient count != n_features")
            model._fitted = True
        return model
