"""Statistical contract: assumption semantics and decision-record content.

The key invariant the task demands: the exclusion restriction is recorded as
a caller DECLARATION and is never upgraded from relevance/correlation evidence.
"""

from __future__ import annotations

import pytest

from app.dgp import DGPSpec
from app.service import run_estimation


def _assumption(resp, name):
    return next(a for a in resp.decision.assumptions if a.name == name)


@pytest.mark.contract
def test_exclusion_is_declared_not_inferred_from_strong_relevance(config, build_request):
    spec = DGPSpec(n=5000, n_instruments=3, instrument_strength=1.0, seed=501)
    resp = run_estimation(
        build_request(spec, declared=True, exclusion_rationale="RCT assignment"),
        config,
    )
    excl = _assumption(resp, "exclusion_restriction_E[Z'e]=0")
    assert excl.status.value == "declared"
    assert "cannot be inferred" in excl.detail
    assert "RCT assignment" in excl.detail

    # Relevance is the only instrument property the service marks supported.
    rel = _assumption(resp, "instrument_relevance_rank_condition")
    assert rel.status.value == "supported"


@pytest.mark.contract
def test_undeclared_exclusion_produces_not_assessed_record(config, build_request):
    spec = DGPSpec(n=3000, n_instruments=2, seed=502)
    resp = run_estimation(build_request(spec, declared=False), config)
    excl = _assumption(resp, "exclusion_restriction_E[Z'e]=0")
    assert excl.status.value == "not_assessed"
    # Estimates are still arithmetically produced ...
    assert resp.coefficients
    # ... and the decision state carries the fingerprint and request id.
    assert resp.decision.fingerprint
    assert resp.decision.request_id


@pytest.mark.contract
def test_high_correlation_alone_cannot_make_exclusion_supported(config, build_request):
    # Even an absurdly strong first stage must leave exclusion as "declared".
    spec = DGPSpec(n=10000, n_instruments=4, instrument_strength=2.0, seed=503)
    resp = run_estimation(build_request(spec, declared=True), config)
    cd = [d for d in resp.diagnostics
          if d.name == "cragg_donald_weak_identification"][0].value
    assert cd > 1000  # relevance is overwhelming ...
    excl = _assumption(resp, "exclusion_restriction_E[Z'e]=0")
    assert excl.status.value == "declared"  # ... yet exclusion stays declared


@pytest.mark.contract
def test_decision_key_state_has_required_fields(config, build_request):
    spec = DGPSpec(n=3000, n_instruments=2, seed=504)
    resp = run_estimation(build_request(spec), config)
    state = resp.decision.key_state
    for field in (
        "n_obs", "n_endogenous", "n_excluded_instruments", "rank_design",
        "rank_first_stage", "cragg_donald_f", "weak_cutoff",
        "cutoff_source", "max_instrument_vif", "cov_type",
    ):
        assert field in state, field
    assert resp.decision.reasons  # non-empty human-readable justification


@pytest.mark.contract
def test_decision_accepted_vs_warning_band(config, build_request):
    # Construct a moderately strong instrument landing above the cutoff but
    # inside the 20% marginal band -> accepted_with_warning.
    spec = DGPSpec(n=20000, n_instruments=2, instrument_strength=0.13, seed=505)
    resp = run_estimation(build_request(spec), config)
    cd = resp.decision.key_state["cragg_donald_f"]
    cutoff = resp.decision.key_state["weak_cutoff"]
    if cutoff <= cd < 1.2 * cutoff:
        assert resp.decision.verdict.value == "accepted_with_warning"
    else:
        # If the draw missed the band, at least it must be consistently judged.
        assert resp.decision.verdict.value in {"accepted", "inconclusive"}


@pytest.mark.contract
def test_logs_and_records_never_contain_raw_observations(config, build_request, caplog):
    import logging
    spec = DGPSpec(n=800, n_instruments=2, seed=506)
    req = build_request(spec)
    sentinel_values = set(str(v) for v in list(req.columns["y"])[:5])
    with caplog.at_level(logging.INFO, logger="twosls"):
        run_estimation(req, config)
    logged = " ".join(r.getMessage() for r in caplog.records)
    for val in sentinel_values:
        assert val not in logged
