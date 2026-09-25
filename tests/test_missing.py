import numpy as np
import pytest

from abseq.missing import MissingValueError, apply_missing_strategy


VALUES = np.array([1.0, np.nan, 3.0, np.inf])


class TestMissingStrategies:
    def test_raise_flags_missing(self):
        with pytest.raises(MissingValueError):
            apply_missing_strategy(VALUES, "raise")

    def test_raise_passes_clean_data(self):
        cleaned, n_missing = apply_missing_strategy(np.array([1.0, 2.0]), "raise")
        assert n_missing == 0
        np.testing.assert_array_equal(cleaned, [1.0, 2.0])

    def test_drop_removes_non_finite(self):
        cleaned, n_missing = apply_missing_strategy(VALUES, "drop")
        assert n_missing == 2
        np.testing.assert_array_equal(cleaned, [1.0, 3.0])

    def test_impute_mean_fills_with_observed_mean(self):
        cleaned, n_missing = apply_missing_strategy(VALUES, "impute_mean")
        assert n_missing == 2
        assert cleaned[1] == pytest.approx(2.0)
        assert cleaned[3] == pytest.approx(2.0)

    def test_impute_mean_fails_when_all_missing(self):
        with pytest.raises(MissingValueError):
            apply_missing_strategy(np.array([np.nan, np.nan]), "impute_mean")

    def test_unknown_strategy_rejected(self):
        with pytest.raises(ValueError):
            apply_missing_strategy(VALUES, "median")
