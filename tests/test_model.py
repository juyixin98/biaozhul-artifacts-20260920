"""Tests for the linear regression model."""

import numpy as np
import pytest

from feature_pipeline.errors import NotFittedError
from feature_pipeline.model import LinearRegressionModel


class TestLinearRegressionModel:
    def test_predict_before_fit_raises(self):
        with pytest.raises(NotFittedError):
            LinearRegressionModel().predict(np.array([[1.0]]))

    def test_recovers_perfect_linear_relationship(self):
        rng = np.random.default_rng(0)
        x = rng.normal(size=(200, 2))
        true_coef = np.array([2.0, -3.0])
        y = x @ true_coef + 5.0
        model = LinearRegressionModel().fit(x, y)
        np.testing.assert_allclose(model.coef_, true_coef, atol=1e-8)
        np.testing.assert_allclose(model.intercept_, 5.0, atol=1e-8)

    def test_handles_rank_deficient_design_without_error(self):
        # Duplicate columns + zero-variance feature: rank < n_features.
        x = np.array([
            [1.0, 1.0, 0.0],
            [2.0, 2.0, 0.0],
            [3.0, 3.0, 0.0],
            [4.0, 4.0, 0.0],
        ])
        y = np.array([2.0, 4.0, 6.0, 8.0])
        model = LinearRegressionModel().fit(x, y)
        predictions = model.predict(x)
        np.testing.assert_allclose(predictions, y, atol=1e-8)
        assert model.rank_ < 3

    def test_predict_validates_feature_count(self):
        model = LinearRegressionModel().fit(
            np.array([[1.0, 2.0]]), np.array([3.0])
        )
        with pytest.raises(ValueError, match="feature columns"):
            model.predict(np.array([[1.0]]))

    def test_rejects_non_finite_training_matrix(self):
        with pytest.raises(ValueError, match="finite"):
            LinearRegressionModel().fit(
                np.array([[np.nan]]), np.array([1.0])
            )

    def test_round_trips_through_dict(self):
        x = np.arange(10.0).reshape(-1, 1)
        y = 3.0 * x.ravel() + 1.0
        model = LinearRegressionModel().fit(x, y)
        restored = LinearRegressionModel.from_dict(model.to_dict())
        np.testing.assert_allclose(
            restored.predict(x), model.predict(x), atol=1e-12
        )
