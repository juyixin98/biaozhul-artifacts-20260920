"""Independent tests for event-time alignment and explicit support refusal."""
from __future__ import annotations

import pytest

from app.contracts import EventStudyRequest, FailureCategory, Observation
from app.experiments import load_fixture, to_event_request
from app.service import run_event


def test_single_cohort_event_study_hand_values():
    fx = load_fixture("event_single_cohort.json")
    resp = run_event(to_event_request(fx))

    assert resp.status == "ok"
    assert resp.summary["cohort_period"] == fx.payload["cohort_period"]
    assert resp.summary["normalization_period"] == 2

    got = {p.event_time: p.estimate for p in resp.points}
    for k_str, v in fx.expected["estimates_by_event_time"].items():
        assert got[int(k_str)] == pytest.approx(v), f"event time {k_str}"

    # normalization point exactly zero and flagged
    norm = {p.event_time: p for p in resp.points}[-1]
    assert norm.estimate == 0.0
    assert norm.supported is True
    assert "Normalization" in norm.note

    # pre periods show no effect; post jump +5 persistent
    assert {p.event_time: p.estimate for p in resp.points if p.event_time < 0} == {
        -3: 0.0, -2: 0.0, -1: 0.0
    }
    post0 = {p.event_time: p for p in resp.points}[0]
    assert post0.standard_error is not None
    assert post0.n_treated_objects == 2 and post0.n_control_objects == 2


def test_staggered_adoption_is_refused_not_silently_fit():
    fx = load_fixture("event_staggered_refused.json")
    resp = run_event(to_event_request(fx))

    assert resp.status == "refused"
    assert resp.points == []
    assert any(
        f.category is FailureCategory.EVENT_STAGGER_UNSUPPORTED
        and f.object_ids == ["T1", "T2", "T3_LATE"]
        for f in resp.failures
    )
    cohort_detail = next(
        f.detail["cohorts"]
        for f in resp.failures
        if f.category is FailureCategory.EVENT_STAGGER_UNSUPPORTED
    )
    assert cohort_detail == {"T1": 2, "T2": 2, "T3_LATE": 3}


def _obs(oid, period, y, grp, treated_now):
    return Observation(
        object_id=oid, period=period, y=y,
        treated_group=grp, treated_this_period=treated_now,
    )


def test_event_study_missing_period_excludes_object_for_that_point():
    # cohort at period 2, window -1..0. C2 lacks period 2 (the reference).
    obs = [
        _obs("T1", 1, 10, True, False), _obs("T1", 2, 16, True, True),
        _obs("C1", 1, 8, False, False),  _obs("C1", 2, 9, False, False),
        _obs("C2", 0, 7, False, False),  _obs("C2", 1, 9, False, False),
        # C2 period 2 deliberately omitted: must not be read as zero
    ]
    resp = run_event(EventStudyRequest(
        request_id="evt-miss", observations=obs, min_event_time=-1, max_event_time=0
    ))
    assert resp.status == "ok"
    k0 = {p.event_time: p for p in resp.points}[0]
    # Only C1 is a usable control at k=0 (C2 missing reference period 2)
    assert k0.n_control_objects == 1
    # point: treated (16-10)=6 vs C1 (9-8)=1 -> 5
    assert k0.estimate == pytest.approx(5.0)
    excluded = {e.object_id for e in resp.excluded_records}
    assert "C2" in excluded


def test_event_study_requires_treated_and_control():
    obs = [_obs("T1", 1, 1, True, False), _obs("T1", 2, 2, True, True)]
    resp = run_event(EventStudyRequest(
        request_id="evt-nocontrol", observations=obs, min_event_time=-1, max_event_time=0
    ))
    assert resp.status == "refused"
    assert any(f.category is FailureCategory.DEGENERATE_DESIGN for f in resp.failures)
