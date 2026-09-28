"""Tests for the declared cross-fitting scheme and input boundary validation."""

from __future__ import annotations

import numpy as np
import pytest

from ipwate.errors import CrossfitError, ValidationError
from ipwate.splits import make_stratified_folds


def test_folds_partition_sample_exactly_once():
    a = np.array([0, 1] * 50, dtype=np.int8)
    folds = make_stratified_folds(a, n_splits=5, seed=7)
    all_eval = np.concatenate([f.eval_idx for f in folds])
    assert sorted(all_eval.tolist()) == list(range(100))


def test_folds_are_stratified():
    a = np.array([1] * 80 + [0] * 20, dtype=np.int8)
    folds = make_stratified_folds(a, n_splits=4, seed=1)
    for fold in folds:
        rate = a[fold.eval_idx].mean()
        # Each eval fold should contain ~80% treated, never a single class.
        assert 0.5 < rate < 0.98
        assert set(a[fold.train_idx].tolist()) == {0, 1}


def test_folds_are_deterministic_from_seed():
    a = np.random.default_rng(0).integers(0, 2, size=200).astype(np.int8)
    f1 = make_stratified_folds(a, n_splits=5, seed=123)
    f2 = make_stratified_folds(a, n_splits=5, seed=123)
    f3 = make_stratified_folds(a, n_splits=5, seed=124)
    for a_fold, b_fold, c_fold in zip(f1, f2, f3):
        np.testing.assert_array_equal(a_fold.eval_idx, b_fold.eval_idx)
        assert not np.array_equal(a_fold.eval_idx, c_fold.eval_idx)


def test_train_eval_do_not_overlap():
    a = np.array([0, 1] * 30, dtype=np.int8)
    for fold in make_stratified_folds(a, n_splits=5, seed=3):
        assert not np.intersect1d(fold.train_idx, fold.eval_idx).size


def test_too_many_splits_for_a_class_is_a_validation_error():
    a = np.array([1, 1, 1, 0, 0, 1, 1, 1, 1, 1], dtype=np.int8)
    with pytest.raises(ValidationError) as exc:
        make_stratified_folds(a, n_splits=5, seed=0)
    assert exc.value.code == "validation_error"
    assert exc.value.details["n_untreated"] == 2


def test_non_2d_x_rejected():
    from ipwate.validation import validate_xy

    with pytest.raises(ValidationError) as exc:
        validate_xy(
            np.array([1.0, 2.0, 3.0]),
            np.array([0, 1, 0], dtype=np.int8),
            np.array([1.0, 2.0, 3.0]),
            max_observations=1000,
            max_covariates=10,
        )
    assert exc.value.code == "validation_error"


def test_nan_and_nonbinary_treatment_are_rejected_with_categories():
    from ipwate.validation import validate_xy

    n = 20
    x = np.random.default_rng(1).normal(size=(n, 2))
    y = np.zeros(n)
    a_nan = np.ones(n)
    a_nan[0] = np.nan
    with pytest.raises(ValidationError) as exc:
        validate_xy(x, a_nan, y, max_observations=1000, max_covariates=10)
    assert exc.value.code == "validation_error"

    a_bad = np.array([2] + [0] * (n - 1), dtype=float)
    with pytest.raises(ValidationError) as exc:
        validate_xy(x, a_bad, y, max_observations=1000, max_covariates=10)
    assert exc.value.details["example_bad_values"] == [2.0]


def test_tiny_sample_and_single_class_rejected():
    from ipwate.validation import validate_xy

    with pytest.raises(ValidationError):
        validate_xy(
            np.zeros((5, 2)), np.zeros(5, dtype=np.int8), np.zeros(5),
            max_observations=1000, max_covariates=10,
        )
    a = np.ones(20, dtype=np.int8)  # no untreated
    with pytest.raises(ValidationError) as exc:
        validate_xy(np.zeros((20, 2)), a, np.zeros(20),
                    max_observations=1000, max_covariates=10)
    assert exc.value.details["n_untreated"] == 0
