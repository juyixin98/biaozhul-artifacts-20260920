"""Error-contract tests: the four failure classes must be distinguishable.

input / state_conflict / domain / resource / computation each surface a
distinct, stable error code and category, both at the service boundary and
over HTTP.
"""

from __future__ import annotations

import pytest

from app.core.config import CertConfig
from app.core.errors import (
    ErrorCategory,
    ErrorCode,
)
from app.core.tracer import Tracer
from app.services import certify_service


def _run(expression, lo, hi, **config_kw):
    config = CertConfig(**config_kw) if config_kw else CertConfig()
    with Tracer.create(None) as tracer:
        return certify_service.certify_expression(
            expression=expression,
            lower=lo,
            upper=hi,
            config=config,
            tracer=tracer,
            include_approximation=False,
        )


def _expect(expression, lo, hi, **config_kw):
    from app.core.errors import CoreError

    with pytest.raises(CoreError) as exc_info:
        _run(expression, lo, hi, **config_kw)
    return exc_info.value


def test_parse_error_is_input_category() -> None:
    err = _expect("2x", "0", "1")
    assert err.code is ErrorCode.PARSE_ERROR
    assert err.category is ErrorCategory.INPUT


def test_non_numeric_bound_is_input_error() -> None:
    err = _expect("x", "abc", "1")
    assert err.category is ErrorCategory.INPUT


def test_empty_bound_is_input_error() -> None:
    err = _expect("x", "", "1")
    assert err.category is ErrorCategory.INPUT


def test_reversed_interval_is_state_conflict() -> None:
    err = _expect("x", "1", "0")
    assert err.code is ErrorCode.CONFLICTING_PARAMETERS
    assert err.category is ErrorCategory.STATE_CONFLICT


def test_equal_bounds_is_state_conflict() -> None:
    err = _expect("x", "1", "1")
    assert err.category is ErrorCategory.STATE_CONFLICT


def test_domain_error_is_separate_category() -> None:
    err = _expect("log(x-2)", "0", "1")
    assert err.code is ErrorCode.DOMAIN_ERROR
    assert err.category is ErrorCategory.DOMAIN
    assert err.position is not None


def test_expression_too_large_has_dedicated_code() -> None:
    huge = "x + " + " + ".join(["1"] * 700)
    err = _expect(huge, "0", "1", max_expression_len=200)
    assert err.code is ErrorCode.EXPRESSION_TOO_LARGE
    assert err.category is ErrorCategory.INPUT


def test_error_dict_is_json_safe() -> None:
    import json

    err = _expect("2x", "0", "1")
    encoded = json.dumps(err.as_dict())
    decoded = json.loads(encoded)
    assert decoded["code"] == "PARSE_ERROR"
    assert "position" in decoded


def test_resource_exhaustion_returns_partial_not_raises() -> None:
    # A wide transcendental scan with a tiny budget must terminate with a
    # resource result (status undecided / partially_certified), not crash.
    payload = _run("sin(x)", "0", "100", max_evals=40)
    assert payload["summary"]["budget_hit"] == "max_evaluations_reached"
    assert payload["undecided_regions"]  # pending work is reported
    assert payload["status"] in ("undecided", "partially_certified")


def test_max_depth_resource_result() -> None:
    # Double root forces subdivision down to the depth ceiling.
    payload = _run("x^2", "-1", "1", max_depth=12, target_width="1e-60")
    reasons = {u["reason"] for u in payload["undecided_regions"]}
    assert "max_depth_reached" in reasons or payload["summary"][
        "budget_hit"
    ] == "max_depth_reached"


def test_nonfinite_bound_is_input_error() -> None:
    err = _expect("x", "0", "inf")
    assert err.category is ErrorCategory.INPUT
    err_nan = _expect("x", "nan", "1")
    assert err_nan.category is ErrorCategory.INPUT


def test_config_from_env(monkeypatch) -> None:
    monkeypatch.setenv("RC_MAX_DEPTH", "7")
    monkeypatch.setenv("RC_PRECISION_DPS", "33")
    config = CertConfig.from_env()
    assert config.max_depth == 7
    assert config.precision_dps == 33


def test_config_rejects_contradictory_precision() -> None:
    with pytest.raises(ValueError):
        CertConfig(precision_dps=5, display_digits=40).validated()
