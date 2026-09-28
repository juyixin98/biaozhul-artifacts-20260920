"""Boundary validation tests: malformed input -> explicit error categories."""
from __future__ import annotations

import numpy as np
import pytest

from twosls.errors import ValidationError
from twosls.estimator import estimate
from conftest import make_request, sample_to_columns

pytestmark = pytest.mark.unit


def test_missing_column_rejected(strong_sample):
    req = make_request(strong_sample, request_id="missing")
    del req.columns["z1"]
    with pytest.raises(ValidationError) as exc:
        estimate(req)
    assert exc.value.code == "VALIDATION_FAILED"
    assert "z1" in exc.value.key_state["missing"]


def test_ragged_columns_rejected(strong_sample):
    req = make_request(strong_sample, request_id="ragged")
    req.columns["z1"].pop()
    # pydantic schema also validates equal lengths; either boundary may fire
    with pytest.raises(Exception):
        estimate(req)


def test_non_finite_values_rejected(strong_sample):
    req = make_request(strong_sample, request_id="nan")
    req.columns["y"][0] = float("nan")
    with pytest.raises(ValidationError) as exc:
        estimate(req)
    assert exc.value.key_state["matrix"] == "y"
    assert exc.value.key_state["non_finite_cells"] >= 1


def test_infinite_values_rejected(strong_sample):
    req = make_request(strong_sample, request_id="inf")
    req.columns["y"][1] = float("inf")
    with pytest.raises(ValidationError):
        estimate(req)


def test_role_overlap_rejected(strong_sample):
    from twosls.contract import ModelSpec

    req = make_request(strong_sample, request_id="overlap")
    req.spec = ModelSpec(
        dependent="y",
        endogenous=["x_end"],
        included_exogenous=["const", "z1"],  # z1 is also an instrument
        excluded_instruments=["z1", "z2"],
    )
    with pytest.raises(ValidationError) as exc:
        estimate(req)
    assert "z1" in exc.value.key_state["column"]


def test_too_few_observations_rejected(strong_sample):
    req = make_request(strong_sample, request_id="few")
    req.columns = {k: v[:5] for k, v in req.columns.items()}
    with pytest.raises(ValidationError) as exc:
        estimate(req)
    assert exc.value.key_state["nobs"] == 5


def test_zero_variance_endogenous_rejected(strong_sample):
    req = make_request(strong_sample, request_id="zero-var")
    req.columns["x_end"] = [3.0] * len(req.columns["x_end"])
    with pytest.raises(ValidationError):
        estimate(req)


def test_request_id_propagates_into_error(strong_sample):
    req = make_request(strong_sample, request_id="trace-xyz-42")
    del req.columns["z2"]
    try:
        estimate(req)
    except ValidationError as exc:
        assert exc.request_id == "trace-xyz-42"
        assert exc.to_dict()["error"]["request_id"] == "trace-xyz-42"
    else:  # pragma: no cover
        pytest.fail("expected ValidationError")


def test_empty_payload_rejected():
    from twosls.contract import EstimationRequest, EstimationOptions, ModelSpec

    with pytest.raises(Exception):
        EstimationRequest(
            request_id="empty",
            columns={},
            spec=ModelSpec(
                dependent="y", endogenous=["x"], included_exogenous=["const"],
                excluded_instruments=["z"],
            ),
            options=EstimationOptions(),
        )
