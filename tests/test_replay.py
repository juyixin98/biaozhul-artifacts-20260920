"""Tests for the command-line replay tool."""
from __future__ import annotations

import json

from rationalsvc import replay


def _run(argv, log_path):
    return replay.main(argv + ["--log", str(log_path)])


def test_list_last_and_recompute(tmp_path, case):
    log = tmp_path / "log.jsonl"
    # Produce a real run through the runner against this log.
    from rationalsvc import numeric_input, runner
    parsed = numeric_input.parse_request(case("big_common_factor"))
    runner.solve_system(parsed, runner.RunLogger(str(log)), run_id="run-cli-1",
                        include_float_diagnosis=False)

    assert _run(["--list"], log) == 0
    # --last replays and exits 0, printing JSON containing the exact solution
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _run(["--last", "--no-float-diagnosis"], log)
    assert rc == 0
    body = json.loads(buf.getvalue())
    assert body["solutions"][0]["particular"] == ["2", "1"]
    assert body["run_id"] == "replay-run-cli-1"


def test_replay_with_tighter_budget_reports_budget_error(tmp_path, case):
    log = tmp_path / "log.jsonl"
    from rationalsvc import numeric_input, runner
    parsed = numeric_input.parse_request(case("big_common_factor"))
    runner.solve_system(parsed, runner.RunLogger(str(log)), run_id="run-cli-2",
                        include_float_diagnosis=False)

    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _run(["run-cli-2", "--digit-budget", "1"], log)
    assert rc == 2
    body = json.loads(buf.getvalue())
    assert body["error"] == "budget_exhausted"
    assert "progress" in body["details"]


def test_unknown_run_id_exits_nonzero(tmp_path):
    log = tmp_path / "nope.jsonl"
    assert replay.main(["run-ghost", "--log", str(log)]) == 1
