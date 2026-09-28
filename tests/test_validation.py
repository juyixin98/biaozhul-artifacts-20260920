"""Boundary validation failure categories."""

import numpy as np
import pytest

from ipw_ate.errors import DataValidationError, InsufficientDataError
from ipw_ate.pipeline import run_ipw, validate_observations


def test_accepts_well_formed_data():
    t = np.array([0, 1, 0, 1, 1, 0])
    y = np.arange(6, dtype=float)
    x = np.arange(12, dtype=float).reshape(6, 2)
    obs = validate_observations(t, y, x)
    assert obs.n == 6 and obs.p == 2
    assert obs.feature_names == ("x0", "x1")


def test_rejects_non_binary_treatment():
    t = np.array([0, 1, 2, 1, 0, 1])
    with pytest.raises(DataValidationError) as exc:
        validate_observations(t, np.zeros(6), np.ones((6, 1)))
    assert exc.value.code == "data_validation_error"


def test_rejects_shape_mismatch():
    with pytest.raises(DataValidationError):
        validate_observations(np.zeros(5), np.zeros(6), np.ones((6, 1)))


def test_rejects_non_finite_values():
    x = np.ones((6, 1)); x[0, 0] = np.nan
    with pytest.raises(DataValidationError):
        validate_observations(np.array([0, 1] * 3), np.zeros(6), x)


def test_rejects_too_few_units():
    with pytest.raises(InsufficientDataError):
        validate_observations(np.array([0, 1, 1]), np.zeros(3), np.ones((3, 1)))


def test_rejects_no_covariates():
    with pytest.raises(DataValidationError):
        validate_observations(np.array([0, 1, 1, 0]), np.zeros(4),
                              np.empty((4, 0)))


def test_pipeline_requires_units_per_arm_per_fold():
    # Only 2 treated but 5 splits requested -> insufficient.
    t = np.array([1, 1, 0, 0, 0, 0, 0, 0, 0, 0])
    y = np.zeros(10)
    x = np.arange(20, dtype=float).reshape(10, 2)
    with pytest.raises(InsufficientDataError) as exc:
        run_ipw(t, y, x, config=None, request_id="x")
    assert exc.value.code == "insufficient_data_error"
