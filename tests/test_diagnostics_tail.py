"""Tests for diagnostics, tail closure, and audit-event replay."""
from __future__ import annotations

import pytest

from stratblock.contract import AllocationError, ErrorCategory, build_study_config
from stratblock.diagnostics import diagnose_study
from stratblock.replay import (
    ScriptRequest,
    replay_from_audit,
    run_script,
    tail_closure_experiment,
)
from stratblock.rng import draw_next


def _requests(stratum_values, prefix="S"):
    return [
        ScriptRequest(
            subject_id=f"{prefix}{i}",
            features={"site": v},
            request_id=f"req-{prefix.lower()}-{i}",
        )
        for i, v in enumerate(stratum_values)
    ]


@pytest.mark.integration
def test_diagnostics_passes_clean_run_and_reports_block_counts(
    store, two_arm_cfg
) -> None:
    requests = _requests(["A"] * 8 + ["B"] * 5)
    run_script(two_arm_cfg, 20260927, requests, store)
    report = diagnose_study(store, two_arm_cfg.study_id)

    assert report["conclusion"] == "PASS"
    assert report["failures"] == []
    assert report["n_subjects"] == 13
    assert report["n_strata"] == 2
    for stratum in report["strata"]:
        assert stratum["sequence_replay"]["production_kernel_agrees"] is True
        assert stratum["sequence_replay"]["reference_oracle_agrees"] is True
        for block in stratum["blocks"]:
            if block["complete"]:
                assert block["on_ratio"] is True
        # Provenance recorded per stratum.
        assert stratum["stream_provenance"]["key_fingerprint_sha256"]
        assert "stratblock-stream-v1" in stratum["stream_provenance"]["stream_id"]


@pytest.mark.integration
def test_diagnostics_flags_tail_imbalance_as_uncertainty_not_hidden(
    store, two_arm_cfg
) -> None:
    # A 1:1 size-4 block sealed after 2 enrollments is only balanced as
    # (1,1). Stratum "site=2" is chosen because the independent oracle
    # shows (for study "tail-flag") its first two draws are the same arm
    # -> infeasible prefix.
    cfg = build_study_config(
        "tail-flag", ["control", "treatment"], ["site"],
        block_sizes=[4], allocation_ratio=[1, 1],
    )
    reqs = [
        ScriptRequest("T0", {"site": "2"}, "t-0"),
        ScriptRequest("T1", {"site": "2"}, "t-1"),
    ]
    run_script(cfg, 20260927, reqs, store)
    report = diagnose_study(store, cfg.study_id)
    assert report["conclusion"] == "PASS"  # disclosed behaviour, not a failure
    assert report["uncertainties"], "a (0,2)/(2,0) tail must be disclosed"
    u = report["uncertainties"][0]
    assert u["kind"] == "DISCLOSED_TAIL_IMBALANCE"
    assert u["block"]["complete"] is False
    assert u["block"]["flag"] == "TAIL_BALANCE_VIOLATION"
    assert u["block"]["realised_counts"] == [0, 2]
    assert u["block"]["balance_criterion"] == "feasible_apportionment_set"


@pytest.mark.integration
def test_balanced_prefix_tail_is_always_on_target(store, balanced_cfg) -> None:
    reqs = [
        ScriptRequest(f"B{i}", {"age": "old", "sex": "F"}, f"bp-{i}")
        for i in range(5)  # deliberately not a multiple of every block
    ]
    run_script(balanced_cfg, 7, reqs, store)
    report = diagnose_study(store, balanced_cfg.study_id)
    assert report["conclusion"] == "PASS"
    assert report["uncertainties"] == []
    for block in report["strata"][0]["blocks"]:
        assert block["on_ratio"] is True


@pytest.mark.integration
def test_audit_replay_matches_recorded_allocations(store, two_arm_cfg) -> None:
    requests = _requests(["A", "A", "B", "A", "B", "B", "A"])
    run_script(two_arm_cfg, 20260927, requests, store)
    replay = replay_from_audit(store, two_arm_cfg.study_id)
    assert replay["conclusion"] == "PASS"
    assert replay["events_checked"] == 7
    assert replay["mismatches"] == []


@pytest.mark.integration
def test_replay_detects_a_tampered_arm(store, two_arm_cfg) -> None:
    run_script(two_arm_cfg, 20260927, _requests(["A", "A", "B"]), store)
    # Tamper directly with the audited payload.
    with store._conn:  # noqa: SLF001 - deliberate corruption in test
        row = store._conn.execute(
            "SELECT id, payload_json FROM audit_events "
            "WHERE event_type='SUBJECT_ENROLLED' ORDER BY id LIMIT 1"
        ).fetchone()
        import json

        payload = json.loads(row["payload_json"])
        payload["arm"] = "treatment" if payload["arm"] == "control" else "control"
        store._conn.execute(
            "UPDATE audit_events SET payload_json=? WHERE id=?",
            (json.dumps(payload, sort_keys=True), row["id"]),
        )
    replay = replay_from_audit(store, two_arm_cfg.study_id)
    assert replay["conclusion"] == "FAIL"
    assert replay["mismatches"][0]["kind"] == "SEQUENCE_MISMATCH"
    assert replay["mismatches"][0]["subject_id"] == "S0"


@pytest.mark.integration
def test_tail_closure_blocks_new_enrollments_but_serves_repeats(
    store, two_arm_cfg
) -> None:
    requests = _requests(["A"] * 3)  # odd -> open tail when sealed
    result = tail_closure_experiment(two_arm_cfg, 20260927, requests, store)

    assert result["n_allocated"] == 3
    blocked = result["post_seal_enrollments_blocked"]
    assert blocked and all(b["category"] == "STRATUM_CLOSED" for b in blocked)
    # Repeats still return the frozen original arm.
    for repeat in result["post_seal_repeat_requests"]:
        assert repeat["replayed"] is True
    # The open tail is on record with its stream identity.
    if result["open_blocks_at_seal"]:
        tail = result["open_blocks_at_seal"][0]
        assert tail["sealed"] is True
        assert tail["stream_id"].startswith("stratblock-stream-v1")


@pytest.mark.integration
def test_sealing_study_then_enrolling_via_storage_is_refused(
    store, two_arm_cfg
) -> None:
    run_script(two_arm_cfg, 20260927, _requests(["A"]), store)
    store.seal_study(two_arm_cfg.study_id)
    with pytest.raises(AllocationError) as exc:
        store.enroll(
            two_arm_cfg, 20260927, "NEW", {"site": "A"}, "new-req",
            "enroller", draw_next,
        )
    assert exc.value.category is ErrorCategory.STRATUM_CLOSED
