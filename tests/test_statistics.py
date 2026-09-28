"""Independent tests for the DID pipeline.

Independence: expected values come from (a) hand-authored fixture JSON that
records its arithmetic, and (b) a deliberately tiny third implementation below
(``naive_hand_did``) that recomputes the DID straight from raw rows. Neither uses
the package's estimation kernel. Agreement across three paths -- hand value,
naive loops, kernel, and the independent levels OLS -- is what is asserted.
"""
from __future__ import annotations

import math

import pytest

from app.contracts import (
    DIDRequest,
    FailureCategory,
    Observation,
    Severity,
)
from app.experiments import load_fixture, to_did_request
from app.service import run_did


def _field(row, name):
    """Accept both hand-authored dict rows and pydantic Observation objects."""
    return row[name] if isinstance(row, dict) else getattr(row, name)


def naive_hand_did(observations, pre, post):
    """Reference implementation written only for tests: raw dict bookkeeping."""
    pairs: dict[str, dict] = {}
    groups = {}
    for o in observations:
        oid = _field(o, "object_id")
        pairs.setdefault(oid, {})[_field(o, "period")] = _field(o, "y")
        groups[oid] = bool(_field(o, "treated_group"))
    bal = {i: p for i, p in pairs.items() if pre in p and post in p}
    t = [p[post] - p[pre] for i, p in bal.items() if groups[i]]
    c = [p[post] - p[pre] for i, p in bal.items() if not groups[i]]
    means = {}
    for name, ids in (
        ("treated", [i for i in bal if groups[i]]),
        ("control", [i for i in bal if not groups[i]]),
    ):
        ys_t = [pairs[i][pre] for i in ids]
        ys_p = [pairs[i][post] for i in ids]
        means[name] = (sum(ys_t) / len(ys_t), sum(ys_p) / len(ys_p))
    return {
        "did": sum(t) / len(t) - sum(c) / len(c),
        "treated_pre": means["treated"][0],
        "treated_post": means["treated"][1],
        "control_pre": means["control"][0],
        "control_post": means["control"][1],
        "n": len(bal),
    }


# --------------------------------------------------------------------------- #
# 1. Hand-computed four-cell means and DID decomposition
# --------------------------------------------------------------------------- #
def test_handcalc_2x2_exact_values():
    fx = load_fixture("handcalc_2x2.json")
    resp = run_did(to_did_request(fx))

    assert resp.status == "ok"
    d = resp.decomposition
    exp = fx.expected

    # exact four cell means
    assert d.treated_pre.mean == pytest.approx(exp["treated_pre_mean"])
    assert d.treated_post.mean == pytest.approx(exp["treated_post_mean"])
    assert d.control_pre.mean == pytest.approx(exp["control_pre_mean"])
    assert d.control_post.mean == pytest.approx(exp["control_post_mean"])
    assert d.treated_pre.n_objects == 2 and d.control_post.n_objects == 2

    # difference decomposition
    assert d.treated_change == pytest.approx(exp["treated_change"])
    assert d.control_change == pytest.approx(exp["control_change"])
    assert d.did == pytest.approx(exp["did"])

    # hand-derived clustered SE, t and df
    se = resp.clustered_se
    assert se.did_standard_error == pytest.approx(exp["se"], rel=1e-12)
    assert se.t_stat == pytest.approx(exp["t_stat"], rel=1e-12)
    assert se.df == 3
    assert se.n_clusters == 4
    assert se.ci95_low < exp["did"] < se.ci95_high

    # independent naive recomputation from raw rows
    hand = naive_hand_did(fx.payload["observations"], 1, 2)
    assert hand["did"] == pytest.approx(2.0)
    assert hand["treated_pre"] == 11.0 and hand["treated_post"] == 14.5
    assert hand["control_pre"] == 9.0 and hand["control_post"] == 10.5
    assert hand["n"] == 4

    # no exclusions in a clean balanced panel
    assert resp.excluded_records == []
    assert len(resp.excluded_records) == 0


def test_independent_levels_regression_agrees():
    fx = load_fixture("handcalc_2x2.json")
    resp = run_did(to_did_request(fx))
    ref = resp.reference_regression

    # The independent levels path must reproduce the DID, not by construction.
    assert ref.did_coefficient == pytest.approx(2.0, abs=1e-12)
    assert ref.max_abs_did_discrepancy == pytest.approx(0.0, abs=1e-12)
    assert ref.standard_error == pytest.approx(resp.clustered_se.did_standard_error, abs=1e-12)

    # Hand-readable levels coefficients:
    # intercept = control pre mean (9); treated = 11-9 (2);
    # post = control change (1.5); treated_x_post = DID (2)
    coefs = ref.coefficients
    assert coefs["intercept"] == pytest.approx(9.0)
    assert coefs["treated"] == pytest.approx(2.0)
    assert coefs["post"] == pytest.approx(1.5)
    assert coefs["treated_x_post"] == pytest.approx(2.0)


# --------------------------------------------------------------------------- #
# 2. Missing periods are excluded, never treated as zero
# --------------------------------------------------------------------------- #
def test_missing_period_excluded_not_zero():
    fx = load_fixture("missing_period.json")
    req = to_did_request(fx)
    resp = run_did(req)

    assert resp.status == "ok"
    assert resp.decomposition.did == pytest.approx(2.0)
    assert resp.clustered_se.n_objects == 4

    by_id = {e.object_id: e for e in resp.excluded_records}
    assert set(by_id) == {"T3_MISSING_POST", "C3_MISSING_PRE"}
    assert by_id["T3_MISSING_POST"].reasons == [FailureCategory.IDENTITY_MISSING_PERIOD]
    assert by_id["T3_MISSING_POST"].detail["missing_periods"] == [2]
    assert by_id["C3_MISSING_PRE"].detail["missing_periods"] == [1]

    cats = {(f.category, f.severity) for f in resp.failures}
    assert (FailureCategory.IDENTITY_MISSING_PERIOD, Severity.WARNING) in cats

    # naive implementation must also balance to the same four objects
    hand = naive_hand_did(fx.payload["observations"], 1, 2)
    assert hand["n"] == 4 and hand["did"] == pytest.approx(2.0)


# --------------------------------------------------------------------------- #
# 3. Pre-trend: diagnose (and reject), never prove
# --------------------------------------------------------------------------- #
def test_pretrend_nonparallel_is_rejected_but_did_still_reported():
    fx = load_fixture("nonparallel_pretrend.json")
    resp = run_did(to_did_request(fx))
    pt = resp.pretrend
    exp = fx.expected

    assert pt.feasible is True
    assert pt.conclusion == "parallel_trends_rejected"
    assert pt.pretrend_difference == pytest.approx(exp["pretrend_difference"])
    assert pt.standard_error == pytest.approx(exp["pretrend_se"], rel=1e-12)
    assert pt.t_stat == pytest.approx(exp["pretrend_t"], rel=1e-10)

    # rejection surfaces as a dedicated WARNING failure, not an estimate refusal
    assert any(
        f.category is FailureCategory.PRETREND_REJECTED and f.severity is Severity.WARNING
        for f in resp.failures
    )
    assert resp.status == "ok"
    assert resp.decomposition.did == pytest.approx(3.0)


def test_pretrend_without_second_period_is_explicitly_unverifiable():
    fx = load_fixture("handcalc_2x2.json")
    resp = run_did(to_did_request(fx))
    pt = resp.pretrend
    assert pt.feasible is False
    assert pt.conclusion == "not_tested"
    assert "never be proved" in pt.note or "cannot even be diagnosed" in pt.note


def test_pretrend_not_rejected_still_does_not_prove():
    # Parallel-ish pre window with real within-group variation; treatment in p2.
    obs = [
        Observation(object_id="T1", period=0, y=10, treated_group=True, treated_this_period=False),
        Observation(object_id="T1", period=1, y=11, treated_group=True, treated_this_period=False),
        Observation(object_id="T1", period=2, y=15, treated_group=True, treated_this_period=True),
        Observation(object_id="T2", period=0, y=12, treated_group=True, treated_this_period=False),
        Observation(object_id="T2", period=1, y=14, treated_group=True, treated_this_period=False),
        Observation(object_id="T2", period=2, y=19, treated_group=True, treated_this_period=True),
        Observation(object_id="C1", period=0, y=8, treated_group=False, treated_this_period=False),
        Observation(object_id="C1", period=1, y=9, treated_group=False, treated_this_period=False),
        Observation(object_id="C1", period=2, y=10, treated_group=False, treated_this_period=False),
        Observation(object_id="C2", period=0, y=9, treated_group=False, treated_this_period=False),
        Observation(object_id="C2", period=1, y=10, treated_group=False, treated_this_period=False),
        Observation(object_id="C2", period=2, y=11, treated_group=False, treated_this_period=False),
    ]
    req = DIDRequest(
        request_id="test-parallel-ok", observations=obs,
        pre_period=1, post_period=2, earlier_period=0,
    )
    resp = run_did(req)
    assert resp.pretrend.feasible is True
    assert resp.pretrend.t_stat is not None  # finite test with real variation
    assert resp.pretrend.conclusion == "not_rejected"
    assert "NOT prove" in resp.pretrend.note
    assert not any(f.category is FailureCategory.PRETREND_REJECTED for f in resp.failures)


# --------------------------------------------------------------------------- #
# 4. Treatment contamination is excluded
# --------------------------------------------------------------------------- #
def test_contamination_exclusions():
    fx = load_fixture("contamination.json")
    resp = run_did(to_did_request(fx))

    assert resp.contamination.clean is False
    assert resp.contamination.contaminated_control_ids == ["CX_CONTAMINATED"]
    assert resp.contamination.treated_pre_ids == ["TX_EARLY"]

    by_id = {e.object_id: e for e in resp.excluded_records}
    assert FailureCategory.CONTAMINATED_CONTROL in by_id["CX_CONTAMINATED"].reasons
    assert FailureCategory.PRETREND_TREATED_EARLY in by_id["TX_EARLY"].reasons

    cats = [f.category for f in resp.failures]
    assert FailureCategory.CONTAMINATED_CONTROL in cats
    assert FailureCategory.PRETREND_TREATED_EARLY in cats

    # clean-sample DID unaffected by the control outlier 50 or early treated 20
    assert resp.decomposition.did == pytest.approx(2.0)
    assert resp.clustered_se.n_objects == 4

    hand = naive_hand_did(
        [o for o in fx.payload["observations"] if o["object_id"] not in
         ("CX_CONTAMINATED", "TX_EARLY")], 1, 2
    )
    assert hand["did"] == pytest.approx(2.0)


# --------------------------------------------------------------------------- #
# 5. Refusal / failure categories are concrete, not generic errors
# --------------------------------------------------------------------------- #
def _obs(oid, period, y, grp, treated_now):
    return Observation(
        object_id=oid, period=period, y=y,
        treated_group=grp, treated_this_period=treated_now,
    )


def test_refused_when_only_one_group():
    obs = [_obs("T1", 1, 1, True, False), _obs("T1", 2, 2, True, True)]
    resp = run_did(DIDRequest(request_id="r", observations=obs, pre_period=1, post_period=2))
    assert resp.status == "refused"
    assert resp.decomposition is None
    assert any(f.category is FailureCategory.DEGENERATE_DESIGN and
               f.severity is Severity.ERROR for f in resp.failures)


def test_refused_when_period_missing_entirely():
    obs = [_obs("T1", 1, 1, True, False), _obs("C1", 1, 1, False, False)]
    resp = run_did(DIDRequest(request_id="r", observations=obs, pre_period=1, post_period=2))
    assert resp.status == "refused"
    assert any(f.category is FailureCategory.DEGENERATE_DESIGN for f in resp.failures)


def test_duplicate_object_period_excluded():
    obs = [
        _obs("T1", 1, 1, True, False),
        _obs("T1", 1, 9, True, False),
        _obs("T1", 2, 2, True, True),
        _obs("C1", 1, 1, False, False),
        _obs("C1", 2, 1.5, False, False),
    ]
    resp = run_did(DIDRequest(request_id="r", observations=obs, pre_period=1, post_period=2))
    assert resp.status == "refused"
    assert resp.excluded_records[0].object_id == "T1"
    assert resp.excluded_records[0].reasons == [FailureCategory.DUPLICATE_OBJECT_PERIOD]
    assert any(f.category is FailureCategory.DUPLICATE_OBJECT_PERIOD for f in resp.failures)


def test_single_cluster_per_group_flags_degenerate_variance():
    obs = [
        _obs("T1", 1, 0, True, False), _obs("T1", 2, 5, True, True),
        _obs("C1", 1, 0, False, False), _obs("C1", 2, 1, False, False),
    ]
    resp = run_did(DIDRequest(request_id="r", observations=obs, pre_period=1, post_period=2))
    # point identified (DID = 4) but no reliable clustered SE
    assert resp.decomposition.did == pytest.approx(4.0)
    assert math.isnan(resp.clustered_se.did_standard_error)
    assert any(
        f.category is FailureCategory.CLUSTER_VARIANCE_DEGENERATE
        and f.severity is Severity.WARNING
        for f in resp.failures
    )


def test_request_identity_is_echoed():
    resp = run_did(DIDRequest(
        request_id="corr-xyz-123",
        observations=[
            _obs("T1", 1, 0, True, False), _obs("T1", 2, 1, True, True),
            _obs("T2", 1, 0, True, False), _obs("T2", 2, 2, True, True),
            _obs("C1", 1, 0, False, False), _obs("C1", 2, 0, False, False),
            _obs("C2", 1, 0, False, False), _obs("C2", 2, 1, False, False),
        ],
        pre_period=1, post_period=2,
    ))
    assert resp.request_id == "corr-xyz-123"
    assert resp.version and resp.service
    assert "processing_location" in resp.summary
