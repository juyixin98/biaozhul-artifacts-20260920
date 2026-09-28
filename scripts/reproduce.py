#!/usr/bin/env python3
"""Reproduce all hand-authored experiments and print a verification report.

Usage:  python scripts/reproduce.py

The expected values are read from fixtures/*.json (hand-authored); this script
only runs the core and compares. It exits non-zero if any check fails.
"""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.experiments import (  # noqa: E402
    all_did_fixtures,
    all_event_fixtures,
    to_did_request,
    to_event_request,
)
from app.service import run_did, run_event  # noqa: E402

TOL = 1e-9
ok = True


def check(label, actual, expected):
    global ok
    good = actual == expected
    flag = "PASS" if good else "FAIL"
    if not good:
        ok = False
    print(f"  [{flag}] {label}: core={actual!r} expected={expected!r}")


def check_near(label, actual, expected):
    global ok
    good = abs(actual - expected) <= TOL
    flag = "PASS" if good else "FAIL"
    if not good:
        ok = False
    print(f"  [{flag}] {label}: core={actual:.12g} expected={expected:.12g}")


def main() -> int:
    print("== DID experiments ==")
    for fx in all_did_fixtures():
        print(f"\n* {fx.case_id}: {fx.payload['description'][:80]}")
        resp = run_did(to_did_request(fx))
        exp = fx.expected
        if "did" in exp:
            check_near("DID", resp.decomposition.did, exp["did"])
        if "n_objects" in exp:
            check("n_objects", resp.clustered_se.n_objects, exp["n_objects"])
        if "se" in exp:
            check_near("clustered SE", resp.clustered_se.did_standard_error, exp["se"])
        if "pretrend_conclusion" in exp:
            check(
                "pretrend conclusion",
                resp.pretrend.conclusion,
                exp["pretrend_conclusion"],
            )
            check_near(
                "pretrend difference",
                resp.pretrend.pretrend_difference,
                exp["pretrend_difference"],
            )
        if "contaminated_control_ids" in exp:
            check(
                "contaminated controls",
                sorted(resp.contamination.contaminated_control_ids),
                exp["contaminated_control_ids"],
            )
            check(
                "early treated",
                sorted(resp.contamination.treated_pre_ids),
                exp["treated_pre_ids"],
            )
        if resp.reference_regression is not None:
            check_near(
                "|reference OLS - kernel DID|",
                resp.reference_regression.max_abs_did_discrepancy,
                0.0,
            )
        print(f"  excluded: {[e.object_id for e in resp.excluded_records]}")
        print(f"  failures : {[(f.category.value, f.severity.value) for f in resp.failures]}")

    print("\n== Event-study experiments ==")
    for fx in all_event_fixtures():
        print(f"\n* {fx.case_id}")
        resp = run_event(to_event_request(fx))
        exp = fx.expected
        check("status", resp.status, exp.get("status", "ok"))
        if "estimates_by_event_time" in exp:
            got = {p.event_time: p.estimate for p in resp.points}
            for k, v in exp["estimates_by_event_time"].items():
                check_near(f"event_time {k}", got[int(k)], v)
        if exp.get("failure_category"):
            cats = [f.category.value for f in resp.failures]
            check("failure category present", exp["failure_category"] in cats, True)

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
