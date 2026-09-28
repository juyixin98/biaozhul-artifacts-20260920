"""End-to-end service orchestration tests through the public service entry."""
from __future__ import annotations

import pytest

from app.contracts.models import (
    BalanceStrategy,
    ControlGroup,
    DIDRequest,
    DiagnosticLevel,
    EventStudyRequest,
    FailureCategory,
    WeightPolicy,
)
from app.core.errors import EstimationError
from app.reproducibility.fixtures import build_fixture
from app.service import run_did, run_event_study
from tests import reference_expected as expected


def _did_req(name, *, rid="r1", **over):
    return DIDRequest(request_id=rid, observations=build_fixture(name), **over)


def test_run_did_hand_panel_full_envelope():
    res = run_did(_did_req("hand_2x2"))
    assert res.status == "ok"
    assert res.request_id == "r1"
    assert res.core_version.startswith("did-core")
    assert res.estimate.value == pytest.approx(expected.HAND_2X2["did"], abs=1e-12)
    assert res.estimate.se == pytest.approx(expected.HAND_2X2["se"], abs=1e-10)
    assert res.estimate.ci_low == pytest.approx(expected.HAND_2X2["ci_low"], abs=1e-6)
    assert res.estimate.n_clusters == 4
    # The decomposition and the cross-check diagnostic are both present.
    assert res.decomposition.did == pytest.approx(3.0, abs=1e-12)
    cross = next(d for d in res.diagnostics if d.name == "decomposition_vs_regression")
    assert cross.level is DiagnosticLevel.OK
    # Steps are explainable and reference ingest/align/classify/estimate.
    step_names = [s.step for s in res.steps]
    assert step_names[:3] == ["ingest", "period_select", "identity_align"]
    assert "classify_2x2" in step_names and "estimate" in step_names


def test_run_did_pretrend_indeterminate_with_one_pre_period():
    res = run_did(_did_req("hand_2x2"))
    pt = next(d for d in res.diagnostics if d.name == "pretrend_slope")
    assert pt.level is DiagnosticLevel.INDETERMINATE
    # Uncertain conclusions are separated from failures: status stays ok.
    assert res.status == "ok"


def test_run_did_nonparallel_pretrend_is_failure_level_diagnostic():
    req = DIDRequest(request_id="r2", observations=build_fixture("nonparallel_pretrend"),
                     pre_period=0, post_period=2)
    res = run_did(req)
    pt = next(d for d in res.diagnostics if d.name == "pretrend_slope")
    assert pt.level is DiagnosticLevel.FAILED
    assert pt.p_value is not None and pt.p_value < 0.05


def test_run_did_contamination_rejected_by_default_with_records():
    req = DIDRequest(request_id="r3", observations=build_fixture("contaminated_control"),
                     pre_period=0, post_period=2)
    with pytest.raises(EstimationError) as exc:
        run_did(req)
    assert exc.value.category is FailureCategory.CONTROL_GROUP_CONTAMINATED
    dropped = {e.unit_id for e in exc.value.excluded}
    assert expected.CONTAMINATED_UNIT in dropped


def test_run_did_contamination_can_be_excluded_and_continue():
    req = DIDRequest(request_id="r4", observations=build_fixture("contaminated_control"),
                     pre_period=0, post_period=2, reject_on_contamination=False)
    res = run_did(req)
    assert res.status == "ok"
    assert res.estimate.value == pytest.approx(expected.CONTAMINATED_CLEAN_DID, abs=1e-12)
    assert res.decomposition.cells.n_control == 1


def test_run_did_missing_period_excluded_recorded():
    res = run_did(_did_req("missing_period", rid="r5"))
    excluded = {e.unit_id: e.reason for e in res.excluded}
    assert excluded.get("m1") is FailureCategory.UNBALANCED_PANEL
    assert excluded.get("m2") is FailureCategory.UNBALANCED_PANEL
    assert res.estimate.value == pytest.approx(expected.MISSING_PERIOD_DID, abs=1e-12)


def test_run_did_weighted():
    req = DIDRequest(request_id="r6", observations=build_fixture("weighted_2x2"),
                     weight_policy=WeightPolicy.UNIT_FIXED)
    res = run_did(req)
    assert res.estimate.value == pytest.approx(expected.WEIGHTED_DID, abs=1e-12)


def test_run_event_study_ok_and_refusal():
    req = EventStudyRequest(request_id="r7", observations=build_fixture("staggered_events"),
                            control_group=ControlGroup.NEVER_TREATED,
                            min_event_time=-2, max_event_time=1)
    res = run_event_study(req)
    assert res.status == "ok"
    assert res.reference_period == -1
    by_k = {p.event_time: p.estimate for p in res.points}
    assert by_k[-1] == 0.0
    assert by_k[0] == pytest.approx(expected.EVENT_TAU[0], abs=1e-9)
    assert by_k[1] == pytest.approx(expected.EVENT_TAU[1], abs=1e-9)
    assert by_k[-2] == pytest.approx(expected.EVENT_TAU[-2], abs=1e-9)

    bad = EventStudyRequest(request_id="r8",
                            observations=build_fixture("staggered_events_out_of_support"),
                            control_group=ControlGroup.NEVER_TREATED,
                            min_event_time=-2, max_event_time=2)
    with pytest.raises(EstimationError) as exc:
        run_event_study(bad)
    assert exc.value.category is FailureCategory.OUT_OF_SUPPORT_EVENT_TIME
