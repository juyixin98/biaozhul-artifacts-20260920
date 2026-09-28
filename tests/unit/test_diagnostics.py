"""Diagnostics redaction: sensitive symbol names are hashed unless
name logging is explicitly enabled."""

from app.diagnostics import redact_detail, redact_name


def test_names_hashed_by_default():
    masked = redact_name("patient-zero-location", log_names=False)
    assert masked.startswith("sym:")
    assert "patient" not in masked


def test_names_clear_when_enabled():
    assert redact_name("A", log_names=True) == "A"


def test_detail_redacts_name_fields_only():
    detail = {"name": "secret-assumption", "node": "N1", "accepted": True, "count": 3}
    redacted = redact_detail(detail, log_names=False)
    assert redacted["name"].startswith("sym:")
    assert redacted["node"].startswith("sym:")
    assert redacted["accepted"] is True
    assert redacted["count"] == 3
