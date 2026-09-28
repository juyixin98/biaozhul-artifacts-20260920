"""System-boundary validation: shapes, finiteness, size, duplicate roles."""

from __future__ import annotations

import pytest

from app.dgp import DGPSpec
from app.errors import (
    IncompatibleShapes,
    InsufficientObservations,
    NonFiniteData,
)
from app.data_prep import prepare_data


def _prepare(req, max_obs=200_000):
    return prepare_data(req, max_obs=max_obs)


@pytest.mark.validation
def test_missing_column_is_incompatible_shapes(build_request):
    spec = DGPSpec(n=500, seed=301)
    req = build_request(spec)
    req = req.model_copy(update={"dependent": "does_not_exist"})
    with pytest.raises(IncompatibleShapes) as exc:
        _prepare(req)
    assert exc.value.details["missing_column"] == "does_not_exist"


@pytest.mark.validation
def test_column_used_in_two_roles_rejected(build_request):
    spec = DGPSpec(n=500, seed=302)
    req = build_request(spec, instruments=["z1", "w1"])  # w1 already exogenous
    with pytest.raises(IncompatibleShapes) as exc:
        _prepare(req)
    assert "w1" in str(exc.value)


@pytest.mark.validation
def test_unequal_lengths_rejected(build_request):
    spec = DGPSpec(n=500, seed=303)
    req = build_request(spec)
    cols = dict(req.columns)
    cols["z1"] = cols["z1"][:-5]
    req = req.model_copy(update={"columns": cols})
    with pytest.raises(IncompatibleShapes) as exc:
        _prepare(req)
    assert exc.value.details["column"] == "z1"


@pytest.mark.validation
def test_nan_and_inf_rejected_with_group(build_request):
    spec = DGPSpec(n=500, seed=304)
    for bad, label in [(float("nan"), "nan"), (float("inf"), "inf")]:
        req = build_request(spec)
        cols = dict(req.columns)
        vals = list(cols["y"])
        vals[0] = bad
        cols["y"] = vals
        req = req.model_copy(update={"columns": cols})
        with pytest.raises(NonFiniteData) as exc:
            _prepare(req)
        assert exc.value.details["group"] == "dependent"


@pytest.mark.validation
def test_too_many_observations_rejected(build_request):
    spec = DGPSpec(n=100, seed=305)
    req = build_request(spec)
    with pytest.raises(IncompatibleShapes) as exc:
        _prepare(req, max_obs=50)
    assert exc.value.details["max_obs"] == 50


@pytest.mark.validation
def test_insufficient_observations(build_request):
    spec = DGPSpec(n=3, n_instruments=2, seed=306)
    req = build_request(spec)
    with pytest.raises(InsufficientObservations):
        _prepare(req)


@pytest.mark.validation
def test_exclusion_declaration_is_required_by_schema():
    # Pydantic must refuse a payload that omits the exclusion declaration.
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        from app.contracts import EstimateRequest
        EstimateRequest(
            dependent="y", endogenous=["x1"], instruments=["z1"],
            columns={"y": [1.0], "x1": [1.0], "z1": [1.0]},
            # assume_exclusion_restriction intentionally omitted
        )
