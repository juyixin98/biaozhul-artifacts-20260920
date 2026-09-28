"""Diagnostics tests: redaction and request/record correlation."""
from __future__ import annotations

import io
import json

from provenance.diagnostics import JsonDiagnostics, redact


def test_redact_masks_sensitive_leaves_but_keeps_structure():
    payload = {
        "username": "alice",
        "password": "hunter2",
        "nested": {"api_key": "abc", "ok": True},
        "items": [{"token": "t"}, {"kind": "x"}],
    }
    safe = redact(payload)
    assert safe["username"] == "alice"
    assert safe["password"] == "***REDACTED***"
    assert safe["nested"]["api_key"] == "***REDACTED***"
    assert safe["nested"]["ok"] is True
    assert safe["items"][0]["token"] == "***REDACTED***"
    assert safe["items"][1]["kind"] == "x"


def test_each_decision_is_one_json_line_with_ids_and_state():
    sink = io.StringIO()
    diag = JsonDiagnostics("prov.diag", stream=sink)
    diag.accepted(
        "row ok",
        {"columns": ["a"], "auth_token": "secret"},
        request_id="req-9",
        record_id="rec-1",
    )
    line = sink.getvalue().strip()
    event = json.loads(line)
    assert event["request_id"] == "req-9"
    assert event["record_id"] == "rec-1"
    assert event["outcome"] == "accepted"
    assert event["reason"] == "row ok"
    assert event["state"]["columns"] == ["a"]
    assert event["state"]["auth_token"] == "***REDACTED***"


def test_rejected_and_indeterminable_are_distinct_outcomes():
    sink = io.StringIO()
    diag = JsonDiagnostics("prov.diag2", stream=sink)
    diag.rejected("nope", {"why": "false"}, request_id="r")
    diag.indeterminable("null", {"why": "unknown"}, request_id="r")
    outcomes = [json.loads(line)["outcome"] for line in sink.getvalue().strip().splitlines()]
    assert outcomes == ["rejected", "indeterminable"]
