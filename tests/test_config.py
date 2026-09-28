"""Configuration layer and logger smoke tests."""
from __future__ import annotations

import json
from pathlib import Path

from app.config import Settings


def test_settings_pick_up_environment_overrides(monkeypatch):
    monkeypatch.setenv("SPM_MAX_PATTERN_LENGTH", "7")
    monkeypatch.setenv("SPM_MAX_GAP_POSITION", "42")
    monkeypatch.setenv("SPM_DB_PATH", "/tmp/x.db")
    monkeypatch.setenv("SPM_LOG_DIR", "")  # empty falls back to default
    resolved = Settings.from_env()
    assert resolved.max_pattern_length == 7
    assert resolved.max_gap_position == 42
    assert resolved.db_path == "/tmp/x.db"
    assert resolved.log_dir  # empty string -> default, not empty


def test_settings_are_frozen():
    import dataclasses
    import pytest
    settings = Settings.from_env()
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.db_path = "/other"  # type: ignore[misc]


def test_run_logger_warning_and_close():
    from app.config import settings
    from app.observability import get_run_logger
    logger = get_run_logger("warn-test", prefix="t")
    logger.warning("something_unusual", detail="edge")
    logger.close()
    path = Path(settings.log_dir) / "warn-test.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines() if line]
    warning = next(r for r in records if r["msg"]["event"] == "something_unusual")
    assert warning["level"] == "WARNING"
    assert warning["msg"]["detail"] == "edge"
    assert warning["run_id"] == "warn-test"
