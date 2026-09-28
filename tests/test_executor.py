"""Integration tests: executor against an independent NumPy oracle.

Covers the four mandated scenarios (diamond, long-lived outputs, dynamic shape
jump, concurrent branches) plus aliasing, retention accounting, budget and the
distinguishable failure categories. Expected numbers come from
``fixtures.reference_outputs`` which never uses the code under test.
"""

from __future__ import annotations

import numpy as np
import pytest

from tensor_mem import fixtures as fx
from tensor_mem.errors import (
    ComputationError,
    InputValidationError,
    ResourceExhaustedError,
    StateConflictError,
)
from tensor_mem.executor import Executor
from tensor_mem.runlog import RunLogger


# --------------------------------------------------------------------------- #
# Numeric correctness vs independent oracle
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.parametrize(
    "case_factory",
    [
        fx.diamond_case,
        fx.long_lived_case,
        fx.workspace_case,
        fx.alias_case,
        fx.concurrent_case,
        lambda: fx.dynamic_case(8, 4, 2),
        lambda: fx.dynamic_case(16, 4, 8),
        fx.linear_grad_case,
    ],
)
def test_numeric_results_match_independent_oracle_with_reuse(case_factory, tmp_path):
    case = case_factory()
    log = RunLogger(run_id=f"num-{case.name}", path=tmp_path / "num.jsonl")
    ex = Executor(case.graph(), logger=log)
    handles, report = ex.execute(case.feeds, run_id=f"num-{case.name}")
    ref = fx.reference_outputs(case)
    assert set(handles) == set(ref)
    for name, handle in handles.items():
        assert handle.array.shape == ref[name].shape
        np.testing.assert_allclose(
            handle.array, ref[name], rtol=1e-6, atol=1e-6,
            err_msg=f"case {case.name} output {name} diverged from oracle",
        )
    assert report.replanned in (True, False)
    log.close()
    for h in handles.values():
        h.release()


@pytest.mark.integration
def test_reuse_and_no_reuse_executions_agree_with_each_other_and_oracle():
    case = fx.workspace_case()
    reused = Executor(case.graph(), allow_reuse=True)
    naive = Executor(case.graph(), allow_reuse=False)
    h1, r1 = reused.execute(case.feeds, run_id="reuse-on")
    feeds2 = {k: v.copy() for k, v in case.feeds.items()}
    h2, r2 = naive.execute(feeds2, run_id="reuse-off")
    ref = fx.reference_outputs(case)
    np.testing.assert_allclose(h1["y"].array, ref["y"], rtol=1e-6)
    np.testing.assert_allclose(h2["y"].array, ref["y"], rtol=1e-6)
    # Barriers make the per-wave peak reuse-invariant; reuse shows up in the
    # arena reservation and cumulative bytes acquired: diamond reuses t's slot
    # for y (256B acquired) where no-reuse needs all five records (320B).
    assert reused.plan.total_pool_bytes < naive.plan.total_pool_bytes
    assert r1.total_acquired_bytes < r2.total_acquired_bytes
    assert r2.total_acquired_bytes == naive.plan.no_reuse_total
    for h in list(h1.values()) + list(h2.values()):
        h.release()


# --------------------------------------------------------------------------- #
# Scenario: diamond
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_diamond_reuses_dead_temp_for_join_output():
    case = fx.diamond_case()
    ex = Executor(case.graph())
    handles, report = ex.execute(case.feeds, run_id="diamond-reuse")
    t_slot = ex.plan.slot_of("val::t")
    y_slot = ex.plan.slot_of("val::y")
    assert t_slot == y_slot  # t dies after wave 1; y takes its slot
    assert report.plan_peak_resident == 256
    assert report.wave_resident[1]["resident_bytes"] == 256
    handles["y"].release()


# --------------------------------------------------------------------------- #
# Scenario: long-lived output
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_long_lived_output_stays_valid_across_later_waves():
    case = fx.long_lived_case()
    ex = Executor(case.graph())
    handles, _ = ex.execute(case.feeds, run_id="longlive")
    ref = fx.reference_outputs(case)
    # late waves must not have overwritten the retained early output
    np.testing.assert_allclose(handles["early"].array, ref["early"])
    np.testing.assert_allclose(handles["final"].array, ref["final"])
    early_slot = ex.plan.slot_of("val::early")
    final_slot = ex.plan.slot_of("val::final")
    assert early_slot != final_slot
    handles["early"].release()
    handles["final"].release()


@pytest.mark.integration
def test_retained_outputs_charged_against_later_run_budget():
    case = fx.diamond_case()
    ex = Executor(case.graph())
    h1, r1 = ex.execute(case.feeds, run_id="retain-1")
    assert ex.retained_bytes() == 64
    h2, r2 = ex.execute({k: v.copy() for k, v in case.feeds.items()}, run_id="retain-2")
    assert r2.retained_before_bytes == 64
    assert r2.charged_peak_bytes == r2.plan_peak_resident + 64
    # with a budget that fits one run but not run + retained output, run 2 fails
    tight = Executor(case.graph(), budget=256)
    h3, _ = tight.execute(case.feeds, run_id="retain-3")
    with pytest.raises(ResourceExhaustedError) as exc:
        tight.execute({k: v.copy() for k, v in case.feeds.items()}, run_id="retain-4")
    assert exc.value.details["retained_output_bytes"] == 64
    assert exc.value.details["charged_peak_bytes"] == 320
    for h in (*h1.values(), *h2.values(), *h3.values()):
        h.release()


@pytest.mark.integration
def test_handle_lifecycle_use_after_and_double_release():
    case = fx.diamond_case()
    ex = Executor(case.graph())
    handles, _ = ex.execute(case.feeds, run_id="handle")
    h = handles["y"]
    assert h.released is False
    h.release()
    assert h.released is True
    with pytest.raises(StateConflictError) as exc:
        h.numpy()
    assert exc.value.category == "state_conflict"
    with pytest.raises(StateConflictError):
        h.release()


# --------------------------------------------------------------------------- #
# Scenario: dynamic shape jump
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_dynamic_shape_growth_replans_without_out_of_range_reuse():
    static = fx.build_dynamic_matmul_graph()
    ex = Executor(static)
    static_peak = ex.plan.peak_resident
    assert static_peak == 256  # 4x4 @ 4x2: a64 b32->64 c32->64 y64 ws64 wave mix

    big = fx.dynamic_case(m=16, k=4, n=8)
    handles, report = ex.execute(big.feeds, run_id="dyn-big")
    assert report.replanned is True
    assert report.capacity_deficits, "growing shapes must produce deficits"
    # every deficit is required > capacity, never the reverse silent overflow
    assert all(d["required"] > d["capacity"] for d in report.capacity_deficits)
    np.testing.assert_allclose(
        handles["y"].array, fx.reference_outputs(big)["y"], rtol=1e-6
    )
    assert report.plan_peak_resident > static_peak
    handles["y"].release()

    # same shape again: no replan
    same = fx.dynamic_case(m=16, k=4, n=8, seed=99)
    handles2, report2 = ex.execute(same.feeds, run_id="dyn-same")
    assert report2.replanned is False
    np.testing.assert_allclose(
        handles2["y"].array, fx.reference_outputs(same)["y"], rtol=1e-6
    )
    handles2["y"].release()

    # shrink back to a smaller shape: still no replan and correct results
    small = fx.dynamic_case(m=2, k=4, n=2, seed=7)
    handles3, report3 = ex.execute(small.feeds, run_id="dyn-small")
    assert report3.replanned is False
    np.testing.assert_allclose(
        handles3["y"].array, fx.reference_outputs(small)["y"], rtol=1e-6
    )
    handles3["y"].release()


@pytest.mark.integration
def test_dynamic_shape_rank_change_is_input_error_not_replan():
    static = fx.build_dynamic_matmul_graph()
    ex = Executor(static)
    bad = {
        "a": np.zeros((16, 4, 1), dtype=np.float32),  # rank 3 vs declared 2
        "b": np.zeros((4, 8), dtype=np.float32),
    }
    with pytest.raises(InputValidationError) as exc:
        ex.execute(bad, run_id="dyn-rank")
    assert exc.value.details["feed"] == "a"
    assert exc.value.details["got_rank"] == 3


@pytest.mark.integration
def test_dynamic_shape_dtype_change_is_input_error():
    static = fx.build_dynamic_matmul_graph()
    ex = Executor(static)
    bad = {
        "a": np.zeros((16, 4), dtype=np.float64),
        "b": np.zeros((4, 8), dtype=np.float32),
    }
    with pytest.raises(InputValidationError) as exc:
        ex.execute(bad, run_id="dyn-dtype")
    assert exc.value.details["expected"] == "float32"
    assert "float64" in exc.value.details["got"]


# --------------------------------------------------------------------------- #
# Scenario: concurrent branches
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_concurrent_branches_results_correct_and_storage_disjoint():
    case = fx.concurrent_case(3)
    ex = Executor(case.graph())
    handles, report = ex.execute(case.feeds, run_id="concurrent")
    ref = fx.reference_outputs(case)
    for i in range(3):
        np.testing.assert_allclose(
            handles[f"o{i}"].array, ref[f"o{i}"], rtol=1e-6
        )
    # three branches were scheduled in one wave
    assert report.wave_resident[0]["nodes"] == [
        "n_branch_0", "n_branch_1", "n_branch_2"
    ]
    slots = {ex.plan.slot_of(f"val::o{i}") for i in range(3)}
    assert len(slots) == 3
    # numeric agreement under threads proves no in-use buffer was shared
    for h in handles.values():
        h.release()


@pytest.mark.integration
def test_concurrent_branches_repeated_runs_remain_uncorrupted():
    case = fx.concurrent_case(4)
    ex = Executor(case.graph())
    for run in range(5):
        feeds = {
            k: (v + run).astype(np.float32) for k, v in case.feeds.items()
        }
        handles, _ = ex.execute(feeds, run_id=f"conc-rep-{run}")
        x = feeds["x"]
        for i in range(4):
            np.testing.assert_allclose(
                handles[f"o{i}"].array, x @ feeds[f"w{i}"], rtol=1e-5
            )
        for h in handles.values():
            h.release()


# --------------------------------------------------------------------------- #
# Aliasing
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_view_ops_share_storage_with_source():
    case = fx.alias_case()
    ex = Executor(case.graph())
    handles, _ = ex.execute(case.feeds, run_id="alias")
    assert np.shares_memory(handles["r"].array, handles["z"].array)
    ref = fx.reference_outputs(case)
    np.testing.assert_allclose(handles["z"].array, ref["z"])
    for h in handles.values():
        h.release()


# --------------------------------------------------------------------------- #
# Failure categories
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_missing_feed_is_input_error():
    case = fx.diamond_case()
    ex = Executor(case.graph())
    with pytest.raises(InputValidationError) as exc:
        ex.execute({}, run_id="no-feed")
    assert exc.value.details["feed"] == "x"


@pytest.mark.integration
def test_unknown_feed_is_input_error():
    case = fx.diamond_case()
    ex = Executor(case.graph())
    feeds = dict(case.feeds)
    feeds["extra"] = np.zeros((4, 4), dtype=np.float32)
    with pytest.raises(InputValidationError) as exc:
        ex.execute(feeds, run_id="extra-feed")
    assert exc.value.details["feeds"] == ["extra"]


@pytest.mark.integration
def test_non_finite_output_is_computation_failure():
    case = fx.diamond_case()
    ex = Executor(case.graph())
    feeds = {"x": np.full((4, 4), np.inf, dtype=np.float32)}
    with pytest.raises(ComputationError) as exc:
        ex.execute(feeds, run_id="inf")
    assert exc.value.category == "computation_failure"
    # relu(inf)=inf is already detected at wave 0; if not, a wave-1 branch is
    assert exc.value.details["node"] in {
        "n_relu", "n_add_branch", "n_mul_branch"
    }
    assert exc.value.details["non_finite"] >= 1


@pytest.mark.integration
def test_reshape_element_count_change_at_runtime_is_distinguishable_error():
    # A dynamic concrete shape (same rank) whose numel violates the fixed
    # reshape target must surface a classified error, never an out-of-range
    # write or a silently wrong result.
    from tensor_mem.errors import PlannerError
    from tensor_mem.graph import GraphBuilder

    b = GraphBuilder()
    b.feed("x", "float32", (2, 6))
    b.node("n1", "reshape", ["x"], ["v"], {"shape": [3, 4]})
    b.node("n2", "relu", ["v"], ["y"])
    b.graph_outputs(["y"])
    ex = Executor(b.build())
    with pytest.raises(PlannerError) as exc:
        ex.execute({"x": np.zeros((3, 6), dtype=np.float32)}, run_id="reshape-bad")
    assert exc.value.category in {"input_error", "graph_validation_error"}
    assert exc.value.details["input_numel"] == 18
    assert exc.value.details["output_numel"] == 12


# --------------------------------------------------------------------------- #
# Replayable logs
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_run_log_carries_replay_ids_intermediate_state_and_reasons(tmp_path):
    case = fx.diamond_case()
    path = tmp_path / "replay.jsonl"
    log = RunLogger(run_id="replay-run", path=path)
    ex = Executor(case.graph(), logger=log)
    handles, _ = ex.execute(case.feeds, run_id="replay-run")
    handles["y"].release()
    log.close()

    import json

    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert lines[0]["run_id"] == "replay-run"
    assert [ev["seq"] for ev in lines] == list(range(1, len(lines) + 1))
    kinds = [ev["kind"] for ev in lines]
    assert kinds[0] == "run_start"
    assert "wave_start" in kinds and "wave_end" in kinds
    assert "node_done" in kinds
    assert "outputs_retained" in kinds
    assert kinds[-1] == "outputs_released"
    # retention event must precede completion; release must be last
    assert kinds.index("outputs_retained") < kinds.index("run_complete")
    assert kinds.index("run_complete") < kinds.index("outputs_released")
    # intermediate state needed to replay: feed shapes and per-wave resident
    start = lines[0]
    assert start["data"]["feed_shapes"] == {"x": [4, 4]}
    node_events = [ev for ev in lines if ev["kind"] == "node_done"]
    assert {ev["data"]["op"] for ev in node_events} == {"relu", "add", "mul"}
    assert all("slots" in ev["data"] for ev in node_events)


@pytest.mark.integration
def test_failure_log_distinguishes_categories(tmp_path):
    import json

    path = tmp_path / "fail.jsonl"

    # resource_exhausted: a second run while the first run's output is pinned
    log1 = RunLogger(run_id="budget-run", path=path)
    case = fx.diamond_case()
    tight = Executor(case.graph(), budget=256, logger=log1)
    h, _ = tight.execute(case.feeds, run_id="budget-1")
    with pytest.raises(ResourceExhaustedError):
        tight.execute({k: v.copy() for k, v in case.feeds.items()}, run_id="budget-2")
    h["y"].release()
    log1.close()

    # computation_failure: non-finite kernel output in a separate run id
    log2 = RunLogger(run_id="nan-run", path=path)
    ex2 = Executor(case.graph(), logger=log2)
    with pytest.raises(ComputationError):
        ex2.execute({"x": np.full((4, 4), np.nan, dtype=np.float32)}, run_id="nan")
    log2.close()

    events = [json.loads(line) for line in path.read_text().splitlines()]
    failures = [ev for ev in events if ev["kind"] == "failure"]
    categories = {ev["run_id"]: ev["data"]["category"] for ev in failures}
    assert categories["budget-2"] == "resource_exhausted"
    assert categories["nan"] == "computation_failure"
    # resource failure carries the exact accounting that explains the verdict
    budget_failure = next(
        ev for ev in failures if ev["data"]["category"] == "resource_exhausted"
    )
    assert budget_failure["data"]["charged_peak_bytes"] == 320
    assert budget_failure["data"]["budget_bytes"] == 256
