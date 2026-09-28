"""Integration tests for SQLite persistence semantics."""
from __future__ import annotations

import pytest

from ssp.errors import ValidationError
from ssp.storage import RunStore


@pytest.mark.integration
def test_initialize_is_idempotent(settings):
    store = RunStore(settings)
    store.initialize()
    store.initialize()
    assert store.list_runs() == []


@pytest.mark.integration
def test_save_and_get_success_roundtrip(settings):
    store = RunStore(settings)
    store.initialize()
    record = {
        "plan": {
            "run_id": "run-x",
            "endpoint": "normal",
            "input_fingerprint": "fp-x",
            "versions": {"numpy": "x", "scipy": "y"},
        },
    }
    request_spec = {"alpha": 0.05}
    store.save_success(record, request_spec)
    row = store.get("run-x")
    assert row["status"] == "completed"
    assert row["request_spec"] == request_spec
    assert row["result"]["plan"]["run_id"] == "run-x"
    assert row["error_category"] is None


@pytest.mark.integration
def test_save_failure_keeps_explicit_category(settings):
    store = RunStore(settings)
    store.initialize()
    err = ValidationError("bad input", details={"field": "alpha"})
    store.save_failure("run-f", "normal", "fp-f", {"alpha": 0}, err, {"numpy": "x"})
    row = store.get("run-f")
    assert row["status"] == "failed"
    assert row["error_category"] == "validation_error"
    assert row["result"] is None
    assert "bad input" in row["error_message"]


@pytest.mark.integration
def test_get_unknown_returns_none(settings):
    store = RunStore(settings)
    store.initialize()
    assert store.get("nope") is None
