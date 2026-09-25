"""Tests for individual transformers."""

import numpy as np
import pytest

from feature_pipeline.errors import NotFittedError
from feature_pipeline.transforms import (
    CategoricalImputer,
    NumericImputer,
    OneHotEncoder,
    StandardScaler,
    to_numeric,
)


class TestNumericImputer:
    def test_fills_missing_with_training_mean(self):
        imputer = NumericImputer(strategy="mean")
        imputer.fit(np.array([1.0, 2.0, 3.0, None], dtype=object))
        result = imputer.transform(np.array([None, 10.0], dtype=object))
        np.testing.assert_allclose(result, [2.0, 10.0])

    def test_median_strategy(self):
        imputer = NumericImputer(strategy="median")
        imputer.fit(np.array([1.0, 2.0, 100.0], dtype=object))
        result = imputer.transform(np.array([None], dtype=object))
        np.testing.assert_allclose(result, [2.0])

    def test_constant_strategy_uses_fill_value(self):
        imputer = NumericImputer(strategy="constant", fill_value=-1.0)
        imputer.fit(np.array([1.0, None], dtype=object))
        result = imputer.transform(np.array([None, 5.0], dtype=object))
        np.testing.assert_allclose(result, [-1.0, 5.0])

    def test_transform_before_fit_raises(self):
        with pytest.raises(NotFittedError):
            NumericImputer().transform(np.array([1.0]))

    def test_fit_without_observed_values_raises(self):
        with pytest.raises(ValueError, match="no observed values"):
            NumericImputer(strategy="mean").fit(
                np.array([None, None], dtype=object)
            )

    def test_inference_mean_comes_from_training_not_inference(self):
        imputer = NumericImputer(strategy="mean")
        imputer.fit(np.array([10.0, 20.0], dtype=object))
        # Even if every inference value is missing, training mean is used.
        result = imputer.transform(np.array([None, None, None], dtype=object))
        np.testing.assert_allclose(result, [15.0, 15.0, 15.0])

    def test_round_trips_through_dict(self):
        imputer = NumericImputer(strategy="mean").fit(np.array([2.0, 4.0]))
        restored = NumericImputer.from_dict(imputer.to_dict())
        np.testing.assert_allclose(
            restored.transform(np.array([None], dtype=object)), [3.0]
        )


class TestCategoricalImputer:
    def test_fills_missing_with_training_mode(self):
        imputer = CategoricalImputer(strategy="mode")
        imputer.fit(np.array(["a", "b", "a", None], dtype=object))
        result = imputer.transform(np.array([None, "b"], dtype=object))
        assert list(result) == ["a", "b"]

    def test_mode_tie_is_deterministic_and_lexicographic(self):
        imputer = CategoricalImputer(strategy="mode")
        imputer.fit(np.array(["b", "a"], dtype=object))
        assert imputer.statistic_ == "a"

    def test_constant_strategy(self):
        imputer = CategoricalImputer(strategy="constant", fill_value="unknown")
        imputer.fit(np.array(["a"], dtype=object))
        result = imputer.transform(np.array([None], dtype=object))
        assert list(result) == ["unknown"]

    def test_rejects_non_string_categories(self):
        imputer = CategoricalImputer(strategy="mode")
        with pytest.raises(TypeError, match="must be a string"):
            imputer.fit(np.array(["a", 1.5], dtype=object))

    def test_round_trips_through_dict(self):
        imputer = CategoricalImputer(strategy="mode").fit(
            np.array(["x", "x", "y"], dtype=object)
        )
        restored = CategoricalImputer.from_dict(imputer.to_dict())
        assert list(restored.transform(np.array([None], dtype=object))) == ["x"]


class TestStandardScaler:
    def test_standardizes_to_zero_mean_unit_variance(self):
        scaler = StandardScaler().fit(np.array([2.0, 4.0, 6.0]))
        result = scaler.transform(np.array([2.0, 4.0, 6.0]))
        np.testing.assert_allclose(result.mean(), 0.0, atol=1e-12)
        np.testing.assert_allclose(result.std(ddof=0), 1.0, atol=1e-12)

    def test_zero_variance_column_maps_to_zero_without_division_error(self):
        scaler = StandardScaler().fit(np.array([7.0, 7.0, 7.0]))
        assert scaler.zero_variance_ is True
        result = scaler.transform(np.array([7.0, 7.0]))
        np.testing.assert_allclose(result, [0.0, 0.0])

    def test_uses_training_mean_for_inference_values(self):
        scaler = StandardScaler().fit(np.array([0.0, 10.0]))
        # mean=5, std=5 -> inference value 15 maps to 2.0
        np.testing.assert_allclose(
            scaler.transform(np.array([15.0])), [2.0], atol=1e-12
        )

    def test_rejects_nan_at_fit_time(self):
        with pytest.raises(ValueError, match="NaN"):
            StandardScaler().fit(np.array([1.0, np.nan]))

    def test_round_trips_through_dict_preserving_zero_variance(self):
        scaler = StandardScaler().fit(np.array([3.0, 3.0]))
        restored = StandardScaler.from_dict(scaler.to_dict())
        assert restored.zero_variance_ is True
        np.testing.assert_allclose(
            restored.transform(np.array([3.0, 9.0])), [0.0, 6.0]
        )


class TestOneHotEncoder:
    def test_encodes_known_categories_in_sorted_order(self):
        encoder = OneHotEncoder("city").fit(
            np.array(["west", "east"], dtype=object)
        )
        result = encoder.transform(np.array(["east", "west"], dtype=object))
        expected = np.array([[1.0, 0.0], [0.0, 1.0]])
        np.testing.assert_array_equal(result, expected)
        assert encoder.feature_names == ["city=east", "city=west"]

    def test_unknown_category_at_inference_becomes_all_zeros(self):
        encoder = OneHotEncoder("city").fit(
            np.array(["north", "south"], dtype=object)
        )
        result = encoder.transform(
            np.array(["remote", "north"], dtype=object)
        )
        np.testing.assert_array_equal(
            result, np.array([[0.0, 0.0], [1.0, 0.0]])
        )

    def test_categories_are_learned_from_training_only(self):
        encoder = OneHotEncoder("city").fit(
            np.array(["north"], dtype=object)
        )
        encoder.transform(np.array(["south"], dtype=object))
        assert encoder.categories_ == ["north"]

    def test_rejects_missing_value_at_fit_time(self):
        encoder = OneHotEncoder("city")
        with pytest.raises(ValueError, match="missing value"):
            encoder.fit(np.array(["a", None], dtype=object))

    def test_round_trips_through_dict(self):
        encoder = OneHotEncoder("city").fit(
            np.array(["a", "b"], dtype=object)
        )
        restored = OneHotEncoder.from_dict(encoder.to_dict())
        result = restored.transform(np.array(["zzz"], dtype=object))
        np.testing.assert_array_equal(result, [[0.0, 0.0]])


class TestToNumeric:
    def test_blank_strings_and_none_become_nan(self):
        result = to_numeric(np.array([1, "", None], dtype=object))
        assert result[0] == 1.0
        assert np.isnan(result[1])
        assert np.isnan(result[2])

    def test_boolean_is_rejected(self):
        with pytest.raises(TypeError, match="boolean"):
            to_numeric(np.array([True], dtype=object))

    def test_non_numeric_string_is_rejected(self):
        with pytest.raises(TypeError, match="cannot interpret"):
            to_numeric(np.array(["twelve"], dtype=object))
