"""Tests for the pure-stdlib Student-t implementation against published
t-table values and known normal-limit behaviour."""

import math

import pytest

from abtest.tdist import regularized_incomplete_beta, t_cdf, t_ppf

# Published upper-tail critical values t_{0.975, df} (two-sided 95%).
T_TABLE_0975 = {
    1: 12.7062,
    2: 4.3027,
    5: 2.5706,
    10: 2.2281,
    20: 2.0860,
    30: 2.0423,
    60: 2.0003,
    120: 1.9799,
    1000: 1.9623,
}


@pytest.mark.parametrize("df,expected", T_TABLE_0975.items())
def test_t_ppf_matches_published_table(df, expected):
    assert t_ppf(0.975, df) == pytest.approx(expected, abs=1e-3)


def test_t_ppf_normal_limit():
    # As df -> infinity, t_{0.975} -> z_{0.975} = 1.959964...
    assert t_ppf(0.975, 1e6) == pytest.approx(1.959964, abs=1e-4)


def test_t_cdf_symmetry():
    assert t_cdf(1.5, 17) == pytest.approx(1.0 - t_cdf(-1.5, 17), abs=1e-12)


def test_t_cdf_median_is_zero():
    assert t_cdf(0.0, 7) == pytest.approx(0.5, abs=1e-12)


def test_t_cdf_matches_ppf_roundtrip():
    for df in (1, 3, 10, 100):
        assert t_cdf(t_ppf(0.9, df), df) == pytest.approx(0.9, abs=1e-9)


def test_regularized_incomplete_beta_known_values():
    # I_x(1, 1) = x (uniform CDF)
    assert regularized_incomplete_beta(1.0, 1.0, 0.37) == pytest.approx(0.37)
    # I_x(a, b) at x=0 and x=1
    assert regularized_incomplete_beta(2.0, 3.0, 0.0) == 0.0
    assert regularized_incomplete_beta(2.0, 3.0, 1.0) == 1.0
    # I_0.5(2, 2) = 0.5 by symmetry
    assert regularized_incomplete_beta(2.0, 2.0, 0.5) == pytest.approx(0.5)


def test_invalid_arguments_raise():
    with pytest.raises(ValueError):
        t_ppf(0.0, 10)
    with pytest.raises(ValueError):
        t_ppf(0.5, 0)
    with pytest.raises(ValueError):
        t_cdf(0.0, -3)
    with pytest.raises(ValueError):
        regularized_incomplete_beta(1.0, 1.0, 1.5)


def test_no_nan_in_extreme_df():
    assert math.isfinite(t_ppf(0.999, 1))
    assert math.isfinite(t_ppf(0.001, 1e6))
