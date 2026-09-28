"""Identification, rank, weak-instrument and collinearity failure tests.

Each test asserts a concrete failure CATEGORY and the diagnostic numbers
behind it -- not merely that an exception occurred.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.dgp import DGPSpec, generate, standard_names
from app.kernel import estimate as kernel_estimate
from app.errors import SingularDesign, UnidentifiedModel


def _run_kernel(spec, columns_override=None):
    ds = generate(spec)
    nm = standard_names(spec)
    cols = columns_override if columns_override is not None else ds.columns
    y = np.asarray(cols["y"])
    X = np.column_stack([cols[n] for n in nm["endogenous"]])
    W = np.column_stack([cols[n] for n in nm["exogenous"]])
    Z = np.column_stack([cols[n] for n in nm["instruments"]])
    Q = np.column_stack([np.ones(len(y)), W])
    return kernel_estimate(
        y, X, Q, Z,
        names_endog=tuple(nm["endogenous"]),
        names_exog=tuple(nm["exogenous"]),
        names_instruments=tuple(nm["instruments"]),
        add_constant=True, cov_type="conventional", alpha=0.05,
        rank_rcond=1e-9, run_overid=True, run_endogeneity=True,
    )


@pytest.mark.identification
def test_order_condition_underidentified_lt_k():
    spec = DGPSpec(n=2000, n_endogenous=2, n_instruments=1, seed=201)
    with pytest.raises(UnidentifiedModel) as exc:
        _run_kernel(spec)
    assert "order condition" in str(exc.value)
    assert exc.value.details["n_excluded_instruments"] == 1
    assert exc.value.details["n_endogenous"] == 2


@pytest.mark.identification
def test_rank_condition_fails_when_reduced_form_coefficients_are_rank_deficient():
    # [W, Z] has full column rank (z1, z2 independent of w1), but BOTH
    # instruments move only x1: the projected first stage has rank 1 < K=2.
    rng = np.random.default_rng(202)
    n = 3000
    w1 = rng.normal(size=n)
    z1, z2 = rng.normal(size=n), rng.normal(size=n)
    v1 = rng.normal(size=n)
    # v2 is orthogonal to [1, w1, z1, z2] EXACTLY, so x2's reduced form on the
    # instruments is precisely zero (rank failure, not just weak instruments).
    base = np.column_stack([np.ones(n), w1, z1, z2])
    raw = rng.normal(size=n)
    v2 = raw - base @ np.linalg.lstsq(base, raw, rcond=None)[0]
    e = 0.6 * v2 + np.sqrt(1 - 0.6**2) * rng.normal(size=n)
    x1 = w1 + 0.8 * z1 + 0.8 * z2 + v1
    x2 = w1 + v2                       # no instrument relevance for x2
    y = w1 + x1 + x2 + e
    columns = {"y": y.tolist(), "x1": x1.tolist(), "x2": x2.tolist(),
               "w1": w1.tolist(), "z1": z1.tolist(), "z2": z2.tolist()}

    Y = np.asarray(columns["y"])
    X = np.column_stack([columns["x1"], columns["x2"]])
    Q = np.column_stack([np.ones(n), w1])
    Z = np.column_stack([z1, z2])
    with pytest.raises(UnidentifiedModel) as exc:
        kernel_estimate(
            Y, X, Q, Z,
            names_endog=("x1", "x2"), names_exog=("w1",),
            names_instruments=("z1", "z2"),
            add_constant=True, cov_type="conventional", alpha=0.05,
            rank_rcond=1e-9, run_overid=True, run_endogeneity=True,
        )
    assert "rank condition" in str(exc.value)
    assert exc.value.details["rank_first_stage"] == 1
    assert exc.value.details["n_endogenous"] == 2


@pytest.mark.identification
def test_singular_design_when_instrument_column_constant():
    spec = DGPSpec(n=2000, n_instruments=2, seed=203)
    ds = generate(spec)
    cols = dict(ds.columns)
    cols["z1"] = [7.0] * spec.n  # collinear with the intercept
    with pytest.raises(SingularDesign):
        _run_kernel(spec, columns_override=cols)


@pytest.mark.identification
def test_weak_instruments_are_estimated_but_flagged_inconclusive(config, build_request):
    from app.service import run_estimation
    spec = DGPSpec(n=3000, n_instruments=2, instrument_strength=0.025, seed=204)
    resp = run_estimation(build_request(spec), config)
    # Estimates are still returned (not an HTTP error at service layer)...
    assert resp.status == "estimated_weak"
    # ... but the verdict is inconclusive with the exact failure category.
    assert resp.decision.verdict.value == "inconclusive"
    assert resp.decision.failure_category.value == "weak_instruments"
    cd = [d for d in resp.diagnostics
          if d.name == "cragg_donald_weak_identification"][0]
    assert cd.value < 19.93  # Stock-Yogo 10% critical value for (K=1,L=2)
    assert cd.status.value == "rejected"
    # The decision explains WHY with request id and key state.
    assert resp.decision.request_id
    assert "below weak-ID cutoff" in resp.decision.reasons[0]
    assert resp.decision.key_state["weak_cutoff"] == 19.93


@pytest.mark.identification
def test_collinear_instruments_rank_survives_but_warns_with_vif(config, build_request):
    from app.service import run_estimation
    spec = DGPSpec(n=3000, n_instruments=3, collinear_instruments=True,
                   instrument_strength=0.8, seed=205)
    resp = run_estimation(build_request(spec), config)
    vif = resp.decision.key_state["max_instrument_vif"]
    assert vif > config.vif_warn
    assert resp.decision.failure_category.value in {
        "collinear_instruments", "weak_instruments"
    }
    assert any("collinear" in r for r in resp.decision.reasons)


@pytest.mark.identification
def test_invalid_instrument_rejected_by_sargan_while_relevance_holds(config, build_request):
    from app.service import run_estimation
    spec = DGPSpec(n=6000, n_instruments=3, invalid_instrument=True,
                   invalid_strength=1.2, instrument_strength=0.7, seed=206)
    resp = run_estimation(build_request(spec), config)
    sargan = [d for d in resp.diagnostics if d.name == "sargan"][0]
    # Strong relevance ...
    cd = [d for d in resp.diagnostics
          if d.name == "cragg_donald_weak_identification"][0]
    assert cd.value > 24.58  # (1,3) critical value
    # ... yet the over-id test rejects instrument validity.
    assert sargan.p_value < 0.01
    assert sargan.status.value == "rejected"


@pytest.mark.identification
def test_exact_identification_has_no_overid_test_and_no_error(config, build_request):
    from app.service import run_estimation
    spec = DGPSpec(n=4000, n_instruments=1, instrument_strength=0.8, seed=207)
    resp = run_estimation(build_request(spec, run_overid=True), config)
    overids = [d for d in resp.diagnostics if d.name in {"sargan", "hansen_j"}]
    assert overids == []  # exactly identified: test undefined, silently omitted
