"""Tests for the labelled float diagnostic and run-log replay."""

from __future__ import annotations

import json
from fractions import Fraction

import numpy as np

from rational_linalg.floatdiag import float_diagnosis
from rational_linalg.run_log import RunRegistry

from .conftest import fixture_matrix, fixture_vector, load_fixture


def test_float64_misranks_near_indistinguishable_matrix():
    data = load_fixture("near_float_indistinguishable.json")
    A = fixture_matrix(data["A"])
    diagnosis = float_diagnosis(A, exact_rank=2)
    # The production-grade assertion: float64 really does collapse the rank
    # at the default tolerance NumPy/SciPy users get out of the box.
    assert diagnosis["numpy_rank_default_tol"] == 1
    assert diagnosis["rank_mismatch_vs_exact"] is True
    assert diagnosis["exact_rank"] == 2
    # Entries are integers beyond 2^53 spacing, so float conversion rounds them.
    assert diagnosis["input_entries_rounded_by_float"] is True
    # The second singular value is a rounding artefact (~1) versus ~1e16:
    # tight-tolerance rank flips to 2 only by luck, proving the rank is a
    # tolerance-tuning accident rather than something float establishes.
    tight_ranks = set(diagnosis["numpy_rank_at_explicit_tols"].values())
    assert tight_ranks == {2}
    assert diagnosis["smallest_singular_values"][-1] < 2.0
    # NumPy directly: all four entries share the float64 representation
    # 1e16 -- the +/-1 distinctions no longer exist as representable values.
    Af = np.array(
        [[float(v) for v in row] for row in A], dtype=np.float64
    )
    assert {float(x) for row in Af for x in row} == {1.0e16}


def test_float_solve_succeeds_but_returns_wrong_exact_answer():
    # Classical nearly-singular matrix: float64 ranks it full and solve
    # "succeeds", yet the result differs from the exact rational answer.
    data = load_fixture("near_singular_float_wrong_answer.json")
    A = fixture_matrix(data["A"])
    b = fixture_vector(data["b"])
    diagnosis = float_diagnosis(A, b, exact_rank=2)
    record = diagnosis["float_solve"]
    assert record["solved"] is True
    x = record["float_solution"]
    exact = [Fraction(-72070000), Fraction(108080000)]
    # It got the scale right but fails an exact equality check decisively.
    assert any(abs(xf - float(xe)) > 0.05 for xf, xe in zip(x, exact))
    # The exact answer does satisfy the system to the letter.
    from rational_linalg.evidence import verify_solution

    assert verify_solution(A, b, exact, [])["particular_ok"] is True


def test_run_log_records_events_and_persists_jsonl(log_dir):
    from rational_linalg.service import engine
    from rational_linalg.service.schemas import SolveRequest

    registry = RunRegistry()
    recorder = registry.create("solve", "solve-replay-0001")
    payload = SolveRequest(A=[[1, 1], [1, -1]], b=[3, 1])
    response = engine.solve_system(payload, recorder)
    recorder.flush()

    assert response["run_id"] == "solve-replay-0001"
    # JSONL is replayable from the file alone.
    lines = (log_dir / "runs.jsonl").read_text().strip().splitlines()
    events = [json.loads(line) for line in lines]
    summary = events[-1]
    assert summary["type"] == "run_summary"
    assert summary["status"] == "completed"
    assert summary["result"]["classification"] == "unique"
    kinds = {e["event"] for e in events[:-1]}
    assert "pivot_selected" in kinds
    assert "elimination_done" in kinds

    # The registry retains in-process progress for GET /runs.
    fetched = registry.summary(registry.get("solve-replay-0001"))
    assert fetched["status"] == "completed"
    assert fetched["event_count"] >= 2


def test_budget_failure_log_is_replayable_with_intermediate_state(log_dir):
    from rational_linalg.errors import ResourceExhaustedError
    from rational_linalg.service import engine
    from rational_linalg.service.schemas import SolveRequest

    registry = RunRegistry()
    recorder = registry.create("solve", "solve-budget-0002")
    # Hilbert 6x6 minors quickly exceed a 5-digit budget.
    n = 6
    H = [[f"1/{i + j + 1}" for j in range(n)] for i in range(n)]
    payload = SolveRequest(A=H, b=["1"] * n, digit_budget=5)
    try:
        engine.solve_system(payload, recorder)
        raised = False
    except ResourceExhaustedError as exc:
        recorder.fail(exc.to_dict()["error"])
        recorder.flush()
        raised = True
        assert exc.category == "RESOURCE_EXHAUSTED"
        assert exc.progress["completed_pivots"] >= 1
        assert exc.progress["partial_matrix"]
    assert raised

    summary = [
        json.loads(line)
        for line in (log_dir / "runs.jsonl").read_text().strip().splitlines()
    ][-1]
    assert summary["status"] == "failed"
    assert summary["error"]["category"] == "RESOURCE_EXHAUSTED"
    events = [
        json.loads(line)
        for line in (log_dir / "runs.jsonl").read_text().strip().splitlines()
    ][:-1]
    pivot_events = [e for e in events if e.get("event") == "pivot_selected"]
    assert pivot_events  # the failing run kept key intermediate decisions
