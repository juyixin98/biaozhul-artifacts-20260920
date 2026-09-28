"""Unit tests for diagnostics: request ids, reason codes, redaction."""

from __future__ import annotations

from atms_backend.diagnostics import DecisionRecord, new_request_id, redact


def test_request_ids_are_unique_and_prefixed():
    ids = {new_request_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(i.startswith("req-") for i in ids)


def test_redact_masks_sensitive_nested_values_only():
    payload = {
        "request_id": "req-1",
        "api_token": "super-secret-value",
        "nested": {"password": "hunter2", "node_id": "X", "ok": True},
        "items": [{"secret": "s"}, {"decision": "accepted"}],
    }
    out = redact(payload)
    assert out["request_id"] == "req-1"
    assert out["api_token"] == "***REDACTED***"
    assert out["nested"]["password"] == "***REDACTED***"
    assert out["nested"]["node_id"] == "X"
    assert out["items"][0]["secret"] == "***REDACTED***"
    assert out["items"][1]["decision"] == "accepted"


def test_decision_record_log_dict_is_redacted_and_jsonable():
    rec = DecisionRecord(
        request_id="req-x",
        problem_id="p1",
        node_id="X",
        decision="accepted",
        reason="supported_by_environment",
        supporting_environments=[["A"], ["B"]],
        nogood_blockers=[],
    )
    logged = rec.as_log_dict()
    assert logged["request_id"] == "req-x"
    assert logged["supporting_environments"] == [["A"], ["B"]]
    # Key state explaining the verdict is present.
    assert set(["decision", "reason", "incomplete", "nogoods"]) <= set(logged)
