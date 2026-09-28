"""Diagnostic tests: identification, weak instruments, collinearity, validity.

Each test asserts a *specific numeric/verdict outcome* on a DGP engineered to
produce it -- never merely "the endpoint runs".
"""
from __future__ import annotations

import numpy as np
import pytest

from twosls.errors import UnidentifiedError, WeakInstrumentError
from twosls.estimator import estimate
from conftest import make_request

pytestmark = pytest.mark.diagnostics


# ---------------------------------------------------------------- identification

def test_strong_iv_is_identified(strong_sample):
    out = estimate(make_request(strong_sample, request_id="diag-strong"))
    assert out.status == "ok"
    assert out.identification.order_condition is True
    assert out.identification.rank_condition is True
    assert out.identification.rank_value == 1
    # strong instruments: first-stage F far above the rule-of-thumb 10
    assert out.first_stage[0].f_statistic > 100
    assert out.identification.cragg_donald_statistic > 100


def test_order_condition_failure_when_fewer_instruments_than_endog():
    """L < k: under-identified by the order condition, point estimate withheld."""
    from experiments.dgp import generate_multi_endog, MultiEndogConfig
    from twosls.contract import ModelSpec

    sample = generate_multi_endog(MultiEndogConfig(k_endog=2, n_instruments=3))
    cols = {k: v.tolist() for k, v in sample.columns.items()}
    # supply only ONE of the three instruments -> L=1 < k=2
    spec = ModelSpec(
        dependent="y",
        endogenous=["y_end1", "y_end2"],
        included_exogenous=["const"],
        excluded_instruments=["z1"],
    )
    from twosls.contract import EstimationRequest
    req = EstimationRequest(request_id="order-fail", columns=cols, spec=spec)
    with pytest.raises(UnidentifiedError) as exc:
        estimate(req)
    assert exc.value.code == "NOT_IDENTIFIED"
    assert exc.value.request_id == "order-fail"
    assert any("order condition" in r for r in exc.value.key_state["reasons"])


def test_rank_failure_on_sample_exact_orthogonal_instruments():
    """Z orthogonal to Y in-sample -> rank condition fails despite L >= k."""
    from experiments.dgp import rank_failure_sample

    sample = rank_failure_sample()
    with pytest.raises(UnidentifiedError) as exc:
        estimate(make_request(sample, request_id="rank-fail"))
    assert exc.value.code == "NOT_IDENTIFIED"
    state = exc.value.key_state
    assert state["rank"] < state["rank_required"]
    assert any("rank condition fails" in r for r in state["reasons"])
    # request id and key state travel with the rejection
    assert state["nobs"] == sample.config.nobs


def test_exact_duplicate_instrument_is_rank_deficient():
    from experiments.dgp import duplicate_instrument_sample
    from twosls.config import settings
    from twosls.data import build_data
    from twosls.identification import check_identification, first_stage_diagnostics

    sample = duplicate_instrument_sample()
    with pytest.raises(UnidentifiedError) as exc:
        estimate(make_request(sample, request_id="dup"))
    reasons = " ".join(exc.value.key_state["reasons"])
    assert "rank" in reasons

    # exact inspection of the named collinear pair via the diagnostic directly
    data = build_data(make_request(sample, request_id="dup-inspect"))
    ident = check_identification(data, first_stage_diagnostics(data), settings)
    assert ["z1", "z2"] in ident.collinear_instrument_pairs
    assert ident.status == "unidentified"


def test_population_irrelevance_reported_as_weak_not_crash(underidentified_sample_fixture):
    """pi=0 in population: finite-sample F ~ 1 -> status=weak, estimate returned."""
    out = estimate(make_request(underidentified_sample_fixture, request_id="zero-pi"))
    assert out.status == "weak"
    assert out.first_stage[0].f_statistic < 3.0
    assert out.first_stage[0].partial_r2 < 0.01


# ------------------------------------------------------------------- weak IV

def test_weak_instruments_flagged_with_specific_state(weak_sample):
    out = estimate(make_request(weak_sample, request_id="weak"))
    assert out.status == "weak"
    assert out.first_stage[0].f_statistic < 10
    assert out.identification.cragg_donald_statistic < 10
    joined = " ".join(out.warnings)
    assert "weak instruments" in joined
    assert "conditional first-stage F" in joined


def test_strict_mode_rejects_weak_instruments(weak_sample):
    with pytest.raises(WeakInstrumentError) as exc:
        estimate(make_request(weak_sample, request_id="weak-strict", strict=True))
    assert exc.value.code == "WEAK_INSTRUMENTS"
    assert exc.value.key_state["cragg_donald"] < 10
    assert exc.value.request_id == "weak-strict"


def test_weak_instrument_estimate_is_biased_but_reported(weak_sample):
    """With weak IV + endogeneity the point estimate need not hit truth; the
    service must still return it (non-strict) WITH the weak warning, rather
    than silently presenting it as reliable."""
    out = estimate(make_request(weak_sample, request_id="weak-bias"))
    coef = out.coefficient_map()["x_end"].estimate
    truth = weak_sample.config.beta
    # weak-IV bias: |coef - truth| much larger than the strong-IV error
    from experiments.dgp import strong_iv_sample

    strong = estimate(make_request(strong_iv_sample(), request_id="s"))
    strong_err = abs(strong.coefficient_map()["x_end"].estimate - truth)
    assert abs(coef - truth) > 5 * strong_err
    assert out.warnings  # never presented without warning


# -------------------------------------------------------------- collinearity

def test_near_collinear_instruments_named(collinear_sample):
    out = estimate(make_request(collinear_sample, request_id="collin"))
    assert out.identification.collinear_instrument_pairs  # at least one pair
    pair = out.identification.collinear_instrument_pairs[0]
    assert pair == ["z1", "z2"]
    assert abs(out.identification.instrument_correlation_min) > 0.99


def test_partial_weak_multi_endog_flagged_via_conditional_f():
    """Strong for one endog, weak for the other: marginal F hides this; the
    conditional (Sanderson-Windmeijer) F must expose it."""
    from experiments.dgp import multi_endog_partial_weak_sample

    sample = multi_endog_partial_weak_sample()
    cols = {k: v.tolist() for k, v in sample.columns.items()}
    from twosls.contract import EstimationRequest, InstrumentValidityClaim, ModelSpec

    spec = ModelSpec(
        dependent="y", endogenous=["y_end1", "y_end2"],
        included_exogenous=["const"], excluded_instruments=["z1", "z2", "z3"],
    )
    req = EstimationRequest(
        request_id="partial-weak", columns=cols, spec=spec,
        validity_claim=InstrumentValidityClaim(exclusion_restriction_asserted=True),
    )
    out = estimate(req)
    fmap = {f.endogenous_name: f.effective_f_statistic for f in out.first_stage}
    assert fmap["y_end1"] > 100
    assert fmap["y_end2"] < 10
    assert out.status == "weak"


# ------------------------------------------------------------- over-id / DWH

def test_overid_rejection_when_instrument_invalid():
    from experiments.dgp import invalid_instrument_sample

    sample = invalid_instrument_sample()
    out = estimate(make_request(sample, request_id="invalid"))
    assert out.overidentification.testable is True
    assert out.overidentification.verdict == "reject"
    assert out.overidentification.p_value < 0.001
    assert out.status == "inconclusive"


def test_overid_untestable_when_just_identified(just_identified_sample_fixture):
    out = estimate(make_request(just_identified_sample_fixture, request_id="ji"))
    assert out.overidentification.testable is False
    assert out.overidentification.verdict == "untestable"
    assert out.overidentification.statistic is None
    assert out.overidentification.degrees_of_freedom is None


def test_sargan_size_under_null():
    """With valid instruments the rejection frequency is near 5%."""
    from experiments.dgp import generate_multi_endog, MultiEndogConfig
    from twosls.contract import EstimationRequest, InstrumentValidityClaim, ModelSpec
    import logging
    logging.disable(logging.CRITICAL)

    rejections = 0
    reps = 40
    for seed in range(reps):
        sample = generate_multi_endog(MultiEndogConfig(seed=100 + seed))
        cols = {k: v.tolist() for k, v in sample.columns.items()}
        spec = ModelSpec(
            dependent="y", endogenous=["y_end1", "y_end2"],
            included_exogenous=["const"], excluded_instruments=["z1", "z2", "z3"],
        )
        req = EstimationRequest(
            request_id=f"size-{seed}", columns=cols, spec=spec,
            validity_claim=InstrumentValidityClaim(exclusion_restriction_asserted=True),
        )
        out = estimate(req)
        rejections += out.overidentification.p_value < 0.05
    rate = rejections / reps
    # generous MC band around 0.05 (n=40 -> se ~ 0.035)
    assert 0.0 < rate < 0.20


def test_endogeneity_test_detects_known_endogeneity(strong_sample):
    out = estimate(make_request(strong_sample, request_id="dwh"))
    assert out.endogeneity.verdict == "endogenous"
    assert out.endogeneity.p_value < 1e-10
    # OLS biased upward relative to IV under positive cov(v,e) and positive beta
    assert out.endogeneity.ols_contrast[0] > out.endogeneity.ivs_contrast[0]


def test_endogeneity_not_flagged_when_treatment_exogenous():
    from experiments.dgp import exogenous_treatment_sample

    sample = exogenous_treatment_sample()
    out = estimate(make_request(sample, request_id="exog"))
    assert out.endogeneity.verdict == "not_endogenous"
    assert out.endogeneity.p_value > 0.01
    np.testing.assert_allclose(
        out.endogeneity.ols_contrast, out.endogeneity.ivs_contrast, rtol=0.10
    )


# ----------------------------------------------------------- assumptions block

def test_exclusion_restriction_is_recorded_not_inferred(strong_sample):
    out = estimate(make_request(strong_sample, request_id="assump"))
    excl = out.assumptions["exclusion_restriction"]
    assert excl["asserted_by_caller"] is True
    assert excl["derivable_from_data"] is False
    assert isinstance(excl["rationale"], str) and excl["rationale"]


def test_no_assertion_means_not_assumed(strong_sample):
    out = estimate(make_request(strong_sample, request_id="no-excl", assert_exclusion=False))
    assert out.assumptions["exclusion_restriction"]["asserted_by_caller"] is False
    # a high first-stage F must not be mislabeled as proof of exogeneity
    assert out.first_stage[0].f_statistic > 100
    assert out.assumptions["exclusion_restriction"]["derivable_from_data"] is False
