import math

import numpy as np
import pytest

from abseq.stats import normal_ppf, t_cdf, t_ppf, welch_df, welch_t_interval


class TestNormalPpf:
    def test_known_quantiles(self):
        assert normal_ppf(0.975) == pytest.approx(1.959964, abs=1e-5)
        assert normal_ppf(0.5) == pytest.approx(0.0, abs=1e-12)
        assert normal_ppf(0.025) == pytest.approx(-1.959964, abs=1e-5)

    def test_rejects_out_of_range(self):
        with pytest.raises(ValueError):
            normal_ppf(0.0)
        with pytest.raises(ValueError):
            normal_ppf(1.0)


class TestTDistribution:
    @pytest.mark.parametrize(
        "df,expected",
        [(1, 12.7062), (10, 2.22814), (30, 2.04227), (1000, 1.96234)],
    )
    def test_ppf_matches_reference_tables(self, df, expected):
        assert t_ppf(0.975, df) == pytest.approx(expected, abs=1e-3)

    def test_cdf_roundtrip(self):
        for df in (1, 5, 50):
            x = t_ppf(0.9, df)
            assert t_cdf(x, df) == pytest.approx(0.9, abs=1e-6)

    def test_cdf_symmetry(self):
        assert t_cdf(1.3, 7) == pytest.approx(1.0 - t_cdf(-1.3, 7), abs=1e-9)

    def test_large_df_approaches_normal(self):
        assert t_ppf(0.975, 1e6) == pytest.approx(normal_ppf(0.975), abs=1e-3)


class TestWelchDf:
    def test_equal_variances_balanced(self):
        # With equal variances and sizes, df = 2n - 2.
        assert welch_df(4.0, 50, 4.0, 50) == pytest.approx(98.0)

    def test_zero_variance_is_infinite_df(self):
        assert welch_df(0.0, 10, 0.0, 10) == math.inf


class TestWelchTInterval:
    def test_recovers_known_interval(self):
        rng = np.random.default_rng(7)
        control = rng.normal(0.0, 1.0, size=200)
        treatment = rng.normal(0.5, 1.0, size=200)
        result = welch_t_interval(control, treatment, 0.95)
        assert result["ci_lower"] < 0.5 < result["ci_upper"]
        assert result["mean_difference"] == pytest.approx(
            float(np.mean(treatment) - np.mean(control))
        )
        assert result["ci_upper"] - result["ci_lower"] == pytest.approx(
            2 * t_ppf(0.975, result["df"]) * result["standard_error"]
        )

    def test_requires_two_observations_per_group(self):
        with pytest.raises(ValueError):
            welch_t_interval(np.array([1.0]), np.array([1.0, 2.0]), 0.95)

    def test_rejects_invalid_confidence(self):
        with pytest.raises(ValueError):
            welch_t_interval(np.array([1.0, 2.0]), np.array([1.0, 2.0]), 1.5)

    def test_constant_groups_give_degenerate_interval(self):
        result = welch_t_interval(
            np.full(5, 1.0), np.full(5, 2.5), 0.95
        )
        assert result["ci_lower"] == result["ci_upper"] == 1.5
