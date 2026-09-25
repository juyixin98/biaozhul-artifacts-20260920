"""合成数据可复现性与场景正确性测试。"""
from __future__ import annotations

import numpy as np
import pytest

from drift import synthetic


def test_same_distribution_is_reproducible() -> None:
    b1, c1 = synthetic.same_distribution(seed=123)
    b2, c2 = synthetic.same_distribution(seed=123)
    np.testing.assert_array_equal(b1, b2)
    np.testing.assert_array_equal(c1, c2)


def test_different_seeds_differ() -> None:
    b1, _ = synthetic.same_distribution(seed=1)
    b2, _ = synthetic.same_distribution(seed=2)
    assert not np.array_equal(b1, b2)


def test_shifted_distribution_mean_moves_but_scale_stays() -> None:
    b, c = synthetic.shifted_distribution(
        n_baseline=100000, n_current=100000, mean_shift=2.0, seed=42
    )
    assert b.mean() == pytest.approx(0.0, abs=0.02)
    assert c.mean() == pytest.approx(2.0, abs=0.02)
    assert c.std(ddof=0) == pytest.approx(1.0, abs=0.02)


def test_scaled_distribution() -> None:
    b, c = synthetic.scaled_distribution(
        n_baseline=100000, n_current=100000, scale=3.0, seed=42
    )
    assert c.std(ddof=0) == pytest.approx(3.0, abs=0.05)
    assert c.mean() == pytest.approx(0.0, abs=0.05)


def test_inject_missing_rates_and_immutability() -> None:
    x = np.ones(10000)
    y = synthetic.inject_missing(x, rate=0.3, seed=0)
    assert np.isnan(y).mean() == pytest.approx(0.3, abs=0.02)
    assert not np.isnan(x).any()  # 原数组未被修改
    with pytest.raises(ValueError):
        synthetic.inject_missing(x, rate=1.5)


def test_inject_extremes_land_outside_range() -> None:
    x = np.zeros(10000)
    y = synthetic.inject_extremes(x, rate=0.1, magnitude=42.0, seed=2)
    n_extreme = int(np.isclose(np.abs(y), 42.0).sum())
    assert n_extreme == pytest.approx(1000, abs=100)


def test_small_sample_shape() -> None:
    b, c = synthetic.small_sample(n=10)
    assert b.shape == (10,) and c.shape == (10,)


def test_all_missing_current_shape() -> None:
    b, c = synthetic.all_missing_current(n_current=17)
    assert c.shape == (17,)
    assert np.isnan(c).all()
    assert np.isfinite(b).all()
