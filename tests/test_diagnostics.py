"""Diagnostics: structured evidence and redaction of sensitive values."""

import numpy as np
import pytest

from bucket_sync.diagnostics import Diagnostics, redact_state

pytestmark = pytest.mark.unit


def test_events_carry_record_id_round_worker_outcome_and_reason():
    diag = Diagnostics()
    event = diag.emit(
        round_id=3,
        worker_id="w1",
        outcome="rejected",
        reason="worker_lost",
        detail={"lost_worker": "w1"},
    )
    assert event.record_id.startswith("rec-")
    assert event.round_id == 3
    assert event.worker_id == "w1"
    assert event.outcome == "rejected"
    assert len(diag.events()) == 1
    assert diag.for_round(3)[0] is event
    assert diag.rejections() == [event]


def test_record_ids_are_unique_and_monotonic():
    diag = Diagnostics()
    ids = [diag.emit(round_id=1, worker_id=None, outcome="info", reason="x").record_id
           for _ in range(5)]
    assert len(set(ids)) == 5
    assert ids == sorted(ids)


def test_arrays_are_redacted_to_shape_norm_hash_not_values():
    secret = np.array([1.23456789, -9.87654321])
    redacted = redact_state({"grads": {"b": secret}})
    summary = redacted["grads"]["b"]
    assert summary["kind"] == "ndarray"
    assert summary["shape"] == [2]
    assert set(summary) == {"kind", "shape", "l2_norm", "sha256_12"}
    # No raw scalar or its decimal digits may appear in the rendered form.
    rendered = str(summary)
    assert "1.23456789" not in rendered
    assert "-9.87654321" not in rendered
    # same bytes hash identically; different bytes hash differently
    again = redact_state({"g": secret.copy()})["g"]["sha256_12"]
    other = redact_state({"g": secret + 1.0})["g"]["sha256_12"]
    assert again == summary["sha256_12"]
    assert other != summary["sha256_12"]


def test_scalars_that_are_not_sensitive_pass_through():
    out = redact_state({"round_id": 2, "bucket_index": 1, "n_samples": 7})
    assert out == {"round_id": 2, "bucket_index": 1, "n_samples": 7}


def test_nested_containers_are_traversed():
    out = redact_state({"workers": [{"grad": np.zeros(2)}]})
    assert out["workers"][0]["grad"]["shape"] == [2]


def test_coordinator_rejection_event_explains_the_decision(coordinator, layout):
    from tests.drivers import register_all
    register_all(coordinator, ["w0"])
    desc = coordinator.begin_round(["w0"])
    coordinator.submit_bucket(
        desc["round_id"], "w0", 0, np.zeros(2), 1, "wrong-token",
        np.ones(2, dtype=bool),
    )
    events = coordinator.diagnostics.for_round(desc["round_id"])
    rejected = [e for e in events if e.outcome == "rejected"]
    assert len(rejected) == 1
    e = rejected[0]
    assert e.reason == "wrong_base_generation"
    assert e.detail["expected_token"] == desc["base_token"]
    assert e.detail["got_token"] == "wrong-token"
    assert e.worker_id == "w0"
