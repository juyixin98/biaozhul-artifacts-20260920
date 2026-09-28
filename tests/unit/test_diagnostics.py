"""Unit tests for run identity, fingerprints and structured logging."""
from __future__ import annotations

import json
import logging

from ssp.diagnostics import (
    RunLogger,
    input_fingerprint,
    new_run_id,
    numerical_versions,
)


class TestRunIdentity:
    def test_run_ids_are_unique_and_time_prefixed(self):
        a, b = new_run_id(), new_run_id()
        assert a != b
        assert a.startswith("run-") and b.startswith("run-")

    def test_fingerprint_is_stable_and_input_sensitive(self):
        payload = {"alpha": 0.05, "effect": 0.5, "endpoint": "normal"}
        assert input_fingerprint(payload) == input_fingerprint(dict(payload))
        changed = dict(payload, alpha=0.01)
        assert input_fingerprint(changed) != input_fingerprint(payload)

    def test_versions_include_numeric_stack(self):
        versions = numerical_versions()
        assert {"python", "numpy", "scipy"} <= set(versions)


class TestRunLogger:
    def test_records_carry_run_id_and_fingerprint(self, settings, caplog):
        logger = RunLogger("run-correlate", "fp-1234", settings)
        with caplog.at_level(logging.INFO, logger="ssp.run-correlate"):
            logger.step("some_step", n=32, power=0.807)
        record = caplog.records[-1]
        assert record.run_id == "run-correlate"
        assert record.fingerprint == "fp-1234"
        payload = json.loads(record.getMessage())
        assert payload["event"] == "step:some_step"
        assert payload["n"] == 32
        assert payload["power"] == 0.807

    def test_error_events_are_distinct_from_success(self, settings, caplog):
        logger = RunLogger("run-err", "fp-9", settings)
        with caplog.at_level(logging.ERROR, logger="ssp.run-err"):
            logger.error("run_failed", error_category="exact_cap_exceeded")
        assert caplog.records[-1].levelno == logging.ERROR
        assert "exact_cap_exceeded" in caplog.records[-1].getMessage()

    def test_per_run_log_file_written(self, settings):
        logger = RunLogger("run-file", "fp-file", settings)
        logger.info("file_event", ok=True)
        for handler in logger._logger.handlers:
            handler.flush()
        log_file = settings.log_dir / "run-file.log"
        assert log_file.exists()
        content = log_file.read_text(encoding="utf-8")
        assert "run=run-file" in content and "fp=fp-file" in content
