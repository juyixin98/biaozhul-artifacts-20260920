#!/usr/bin/env python3
"""Example calls against the service layer (no HTTP server required).

Run:

    python3 scripts/example_calls.py

Each example prints the request id, the point estimate with its clustered
standard error, the hand-checkable four-cell decomposition, exclusion records
and diagnostics. Failures are printed with their explicit category rather than
a stack trace.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.contracts.models import DIDRequest, EventStudyRequest, ControlGroup
from app.core.errors import EstimationError
from app.reproducibility.fixtures import build_fixture
from app.service import run_did, run_event_study


def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show_did(name: str, **overrides) -> None:
    hr(f"DID  ·  fixture={name}")
    req = DIDRequest(request_id=f"example-{name}", observations=build_fixture(name), **overrides)
    try:
        res = run_did(req)
    except EstimationError as err:
        print(f"REJECTED  [{err.category.value}]  {err.message}")
        for ex in err.excluded:
            print(f"   excluded: {ex.unit_id} -> {ex.reason.value} ({ex.detail})")
        return
    c = res.decomposition
    e = res.estimate
    print(f"request={res.request_id} core={res.core_version} status={res.status}")
    print(f"  treated means : pre={c.cells.treat_pre:g} post={c.cells.treat_post:g} "
          f"(n={c.cells.n_treat})")
    print(f"  control means : pre={c.cells.control_pre:g} post={c.cells.control_post:g} "
          f"(n={c.cells.n_control})")
    print(f"  changes       : treated={c.treat_change:g} control={c.control_change:g}")
    if e.se is not None:
        print(f"  DID = {e.value:g}   se={e.se:g} (clusters G={e.n_clusters}, dof={e.dof}) "
              f"95% CI [{e.ci_low:.3g},{e.ci_high:.3g}] p={e.p_value:.4g}")
    else:
        print(f"  DID = {e.value:g}   (point estimate only; inference unavailable)")
    for ex in res.excluded:
        print(f"   excluded: {ex.unit_id} -> {ex.reason.value}")
    for d in res.diagnostics:
        print(f"   [{d.level.value:>12}] {d.name}: {d.message}")


def show_event() -> None:
    hr("EVENT STUDY  ·  fixture=staggered_events  (window -2..1)")
    req = EventStudyRequest(
        request_id="example-event",
        observations=build_fixture("staggered_events"),
        control_group=ControlGroup.NEVER_TREATED,
        min_event_time=-2,
        max_event_time=1,
    )
    res = run_event_study(req)
    print(f"request={res.request_id} status={res.status} reference_period=k-1")
    for p in res.points:
        if p.event_time == -1:
            print(f"  k={p.event_time:>2}: 0  (normalized reference)")
        else:
            print(f"  k={p.event_time:>2}: {p.estimate:6.2f}  se={p.se:5.2f}  "
                  f"CI [{p.ci_low:6.2f},{p.ci_high:6.2f}]  units={p.n_units} cohorts={p.n_cohorts}")

    hr("EVENT STUDY  ·  out-of-support request must be REFUSED")
    bad = EventStudyRequest(
        request_id="example-event-oos",
        observations=build_fixture("staggered_events_out_of_support"),
        control_group=ControlGroup.NEVER_TREATED,
        min_event_time=-2,
        max_event_time=2,
    )
    try:
        run_event_study(bad)
    except EstimationError as err:
        print(f"REJECTED  [{err.category.value}]  {err.message}")


if __name__ == "__main__":
    show_did("hand_2x2")
    show_did("missing_period")
    show_did("nonparallel_pretrend", pre_period=0, post_period=2)
    show_did("contaminated_control", pre_period=0, post_period=2)
    show_did("contaminated_control", pre_period=0, post_period=2, reject_on_contamination=False)
    show_did("weighted_2x2")
    show_event()
