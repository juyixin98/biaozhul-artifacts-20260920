"""Engine orchestration tests: equivalence verdicts, automatic replanning and
replayable logs."""
from __future__ import annotations

import numpy as np
import pytest

from tenmem.fixtures import (
    build_diamond,
    build_parallel_branches,
    build_shape_mutate,
    diamond_feeds,
    diamond_reference,
    parallel_feeds,
    parallel_reference,
    shape_mutate_feeds,
    shape_mutate_reference,
)

pytestmark = pytest.mark.integration


def test_engine_run_reports_equivalence_and_savings(engine) -> None:
    outcome = engine.run(build_diamond(8), diamond_feeds(8), run_id="eng-diamond")
    assert outcome.numerically_equivalent is True
    assert outcome.max_abs_diff == 0.0
    assert outcome.replanned is False
    assert outcome.plan.peak_bytes < outcome.no_reuse_peak_bytes
    np.testing.assert_allclose(outcome.outputs["y"], diamond_reference(diamond_feeds(8)["x"]))


def test_engine_threaded_parallel_run(engine) -> None:
    g = build_parallel_branches(12, 3)
    outcome = engine.run(g, parallel_feeds(12), parallel=True, run_id="eng-par")
    assert outcome.numerically_equivalent
    np.testing.assert_allclose(outcome.outputs["m2"], parallel_reference(parallel_feeds(12)["x"]))


def test_engine_auto_replans_on_shape_jump(engine) -> None:
    g = build_shape_mutate(capacity=64, bound_n=32)
    feeds = shape_mutate_feeds(48, 64)
    outcome = engine.run(g, feeds, run_id="eng-replan")
    assert outcome.replanned is True
    assert outcome.replan_reason["category"] == "replanning_required"
    assert outcome.numerically_equivalent
    np.testing.assert_allclose(outcome.outputs["act"], shape_mutate_reference(feeds["data"], 48))


def test_run_log_is_replayable_with_decisions(engine, tmp_path) -> None:
    engine.run(build_diamond(8), diamond_feeds(8), run_id="eng-log")
    records = engine.logger.replay("eng-log")
    kinds = [r["kind"] for r in records]
    # Full replay trail: inputs -> trace -> verdict (no failure/replan here).
    assert "run_start" in kinds
    assert "run_trace" in kinds
    assert "verdict" in kinds
    verdict = next(r for r in records if r["kind"] == "verdict")
    assert verdict["numerically_equivalent"] is True
    start = next(r for r in records if r["kind"] == "run_start")
    assert start["feeds"]["x"]["shape"] == [8]


def test_replan_run_logs_failure_then_replan_then_success(engine) -> None:
    g = build_shape_mutate(capacity=64, bound_n=32)
    engine.run(g, shape_mutate_feeds(48, 64), run_id="eng-replan-log")
    records = engine.logger.replay("eng-replan-log")
    kinds = [r["kind"] for r in records]
    assert kinds.index("run_failure") < kinds.index("replan")
    failure = next(r for r in records if r["kind"] == "run_failure")
    assert failure["category"] == "replanning_required"
    traces = [r for r in records if r["kind"] == "run_trace"]
    # The reuse trace after replanning ends ok.
    reuse_traces = [t for t in traces if t["mode"] == "reuse"]
    assert reuse_traces[-1]["status"] == "ok"


def test_release_reduces_pinned_bytes(engine) -> None:
    outcome = engine.run(build_diamond(8), diamond_feeds(8), run_id="eng-release")
    assert engine.session("eng-release").pinned_bytes() > 0
    engine.release("eng-release", "y")
    assert engine.session("eng-release").pinned_bytes() == 0
