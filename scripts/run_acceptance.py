#!/usr/bin/env python3
"""Acceptance scenario for the point-in-time join, with hand-computed answers.

Covers the three required cases and prints the selected version plus the
rationale for every join cell, then asserts against expectations that were
computed by hand from the scenario data (see EXPECTED below).

Run:  python scripts/run_acceptance.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pitjoin import FeatureRecord, FeatureStore, SpineRow, point_in_time_join

# ---------------------------------------------------------------------------
# Scenario data (timestamps are small integers for easy hand-checking)
# ---------------------------------------------------------------------------
RECORDS = [
    # u1 / click_rate_7d: normal versions, a LATE REVISION of event 200
    # (ingest 260), and a LATE-ARRIVING fact (event 300, ingest 400).
    FeatureRecord("u1", "click_rate_7d", 0.10, event_ts=100, ingest_ts=110),
    FeatureRecord("u1", "click_rate_7d", 0.20, event_ts=200, ingest_ts=215),
    FeatureRecord("u1", "click_rate_7d", 0.99, event_ts=200, ingest_ts=260),  # late revision
    FeatureRecord("u1", "click_rate_7d", 0.30, event_ts=300, ingest_ts=400),  # late arrival
    # u1 / avg_spend_30d: single version.
    FeatureRecord("u1", "avg_spend_30d", 42.0, event_ts=150, ingest_ts=160),
    # u2 / click_rate_7d: TWO VERSIONS OF THE SAME event_ts=100 (a correction
    # ingested at 120 supersedes the original ingested at 105).
    FeatureRecord("u2", "click_rate_7d", 0.50, event_ts=100, ingest_ts=105),
    FeatureRecord("u2", "click_rate_7d", 0.55, event_ts=100, ingest_ts=120),
    # u2 / avg_spend_30d: intentionally absent -> MISSING FEATURE case.
]

SPINE = [
    SpineRow("u1", 250),
    SpineRow("u1", 350),
    SpineRow("u1", 450),
    SpineRow("u2", 110),
    SpineRow("u2", 130),
]
FEATURES = ["click_rate_7d", "avg_spend_30d"]

# ---------------------------------------------------------------------------
# Hand-computed expectations.
#
# (u1, 250) click_rate_7d: event<=250 -> {100/110, 200/215, 200/260};
#   ingest<=250 drops 200/260 (late revision); newest event=200 -> 0.20.
# (u1, 250) avg_spend_30d: 150/160 visible -> 42.0.
# (u1, 350) click_rate_7d: revision 200/260 now visible; arrival 300/400
#   still late (ingest 400>350) -> 0.99 (event 200, ingest 260).
# (u1, 350) avg_spend_30d: -> 42.0.
# (u1, 450) click_rate_7d: arrival 300/400 visible, newest event=300 -> 0.30.
# (u1, 450) avg_spend_30d: -> 42.0.
# (u2, 110) click_rate_7d: only 100/105 visible (correction 100/120 late)
#   -> 0.50.
# (u2, 110) avg_spend_30d: no records -> MISSING.
# (u2, 130) click_rate_7d: both same-event versions visible; tie on
#   event_ts=100 broken by latest ingest=120 -> 0.55.
# (u2, 130) avg_spend_30d: no records -> MISSING.
# ---------------------------------------------------------------------------
EXPECTED = {
    ("u1", 250, "click_rate_7d"): (0.20, 200, 215),
    ("u1", 250, "avg_spend_30d"): (42.0, 150, 160),
    ("u1", 350, "click_rate_7d"): (0.99, 200, 260),
    ("u1", 350, "avg_spend_30d"): (42.0, 150, 160),
    ("u1", 450, "click_rate_7d"): (0.30, 300, 400),
    ("u1", 450, "avg_spend_30d"): (42.0, 150, 160),
    ("u2", 110, "click_rate_7d"): (0.50, 100, 105),
    ("u2", 110, "avg_spend_30d"): (None, None, None),
    ("u2", 130, "click_rate_7d"): (0.55, 100, 120),
    ("u2", 130, "avg_spend_30d"): (None, None, None),
}


def main() -> int:
    store = FeatureStore()
    store.ingest(RECORDS)
    results = point_in_time_join(store, SPINE, FEATURES)

    print("=" * 96)
    print("POINT-IN-TIME JOIN — ACCEPTANCE RUN (hand-computed expectations asserted)")
    print("=" * 96)
    failures = 0
    for r in results:
        key = (r["entity_id"], r["spine_ts"], r["feature"])
        exp_value, exp_event, exp_ingest = EXPECTED[key]
        ok = (
            r["value"] == exp_value
            and r["selected_event_ts"] == exp_event
            and r["selected_ingest_ts"] == exp_ingest
        )
        failures += 0 if ok else 1
        status = "OK  " if ok else "FAIL"
        value = "MISSING" if r["value"] is None else f"{r['value']:.2f}"
        print(
            f"[{status}] entity={r['entity_id']:>3} spine_ts={r['spine_ts']:>4} "
            f"feature={r['feature']:<14} -> {value}"
        )
        print(f"       reason: {r['reason']}")
        print(
            f"       evidence: versions_total={r['versions_total']} "
            f"candidates={r['candidates']} "
            f"rejected_future_event={r['rejected_future_event']} "
            f"rejected_late_ingest={r['rejected_late_ingest']} "
            f"(expected value={exp_value} event_ts={exp_event} ingest_ts={exp_ingest})"
        )
        print("-" * 96)

    print(f"acceptance: {len(results) - failures}/{len(results)} cells match hand-computed expectations")
    if failures:
        print(f"RESULT: FAIL ({failures} mismatches)")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
