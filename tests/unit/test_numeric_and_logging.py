"""Tests for numeric validation helpers and the run logger."""

from __future__ import annotations

import json

import numpy as np
import pytest

from app.core.numeric import (
    assert_arrays_close,
    assert_grad_map_close,
    fingerprint,
    max_abs_diff,
)
from app.service.runlog import RunLogger, new_run_id


@pytest.mark.unit
def test_fingerprint_is_content_stable_and_distinct() -> None:
    a = np.arange(6, dtype=np.float64).reshape(2, 3)
    assert fingerprint(a) == fingerprint(a.copy())
    b = a.copy()
    b[0, 0] += 1e-9
    assert fingerprint(a) != fingerprint(b)


@pytest.mark.unit
def test_max_abs_diff_and_close_assertion() -> None:
    a = np.ones(4)
    b = a + 1e-9
    assert 0.0 < max_abs_diff(a, b) < 1e-8
    abs_d, _ = assert_arrays_close(a, b, what="x", rtol=0, atol=1e-7)
    assert abs_d < 1e-7
    with pytest.raises(AssertionError):
        assert_arrays_close(a, a + 1.0, what="x", rtol=0, atol=1e-9)


@pytest.mark.unit
def test_close_assertion_rejects_shape_mismatch() -> None:
    with pytest.raises(AssertionError):
        assert_arrays_close(np.zeros(2), np.zeros(3), what="x")


@pytest.mark.unit
def test_grad_map_close_checks_keys_and_values() -> None:
    actual = {"W": np.ones(2)}
    assert_grad_map_close(actual, {"W": np.ones(2) + 1e-12})
    with pytest.raises(AssertionError):
        assert_grad_map_close(actual, {"b": np.ones(2)})
    with pytest.raises(AssertionError):
        assert_grad_map_close(actual, {"W": np.ones(2) + 1.0})


@pytest.mark.unit
def test_run_logger_appends_replayable_json_records(tmp_path) -> None:
    logger = RunLogger(tmp_path / "runs.jsonl")
    rid = new_run_id("run")
    logger.record({
        "event": "run_completed", "run_id": rid,
        "intermediate": {"peak": 12, "arr": np.array([1.0, 2.0])},
    })
    lines = (tmp_path / "runs.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["run_id"] == rid
    assert "ts" in rec
    assert rec["intermediate"]["arr"] == [1.0, 2.0]


@pytest.mark.unit
def test_run_id_is_sortable_and_unique() -> None:
    ids = {new_run_id() for _ in range(10)}
    assert len(ids) == 10
    assert all(i.startswith("run-") for i in ids)
