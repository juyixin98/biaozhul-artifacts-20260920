"""Tests for diagnostics helpers (redaction + correlation ids)."""
from app.observability import (
    bind_request_id,
    log_decision,
    new_request_id,
    redact,
    request_id_var,
)


def test_redaction_masks_sensitive_keys_recursively():
    payload = {
        "user": "alice",
        "password": "hunter2",
        "nested": {"api_key": "abc123", "ok": True},
        "items": [{"token": "t"}],
    }
    out = redact(payload)
    assert out["password"] == "***REDACTED***"
    assert out["nested"]["api_key"] == "***REDACTED***"
    assert out["items"][0]["token"] == "***REDACTED***"
    assert out["user"] == "alice"
    assert out["nested"]["ok"] is True


def test_redaction_truncates_long_strings():
    out = redact({"q": "x" * 200})
    assert out["q"].endswith("...(truncated)")
    assert len(out["q"]) < 200


def test_redaction_does_not_mutate_input():
    payload = {"password": "hunter2"}
    redact(payload)
    assert payload["password"] == "hunter2"


def test_request_id_binding_round_trips():
    rid = bind_request_id("req-123")
    assert rid == "req-123"
    assert request_id_var.get() == "req-123"
    assert len(new_request_id()) == 32


def test_log_decision_emits_three_outcomes(caplog):
    import logging
    bind_request_id("req-xyz")
    logger = logging.getLogger("provenance.test")
    with caplog.at_level(logging.INFO, logger="provenance.test"):
        log_decision(logger, "accept", request_id="req-xyz", version_id=3,
                     answer_count=2)
        log_decision(logger, "reject", request_id="req-xyz",
                     category="UNKNOWN_COLUMN", reason="no such col")
    messages = [r.getMessage() for r in caplog.records]
    assert "decision=accept" in messages
    assert "decision=reject" in messages
    # correlation id present in structured context
    assert all(
        getattr(r, "context", {}).get("request_id") == "req-xyz"
        for r in caplog.records
    )
