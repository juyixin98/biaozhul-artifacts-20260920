"""Memory planner tests: liveness, reuse, aliasing, budgets, safety."""

import pytest

from tmem.dag import build_dag
from tmem.planner import (
    BudgetExceeded,
    c_strides,
    num_elements,
    overlapping_live_pairs,
    plan_memory,
)


def _plan(inputs, ops, outputs, **kw):
    dag = build_dag({"inputs": inputs, "ops": ops, "outputs": outputs})
    return dag, plan_memory(dag, **kw)


# ---------------------------------------------------------------------------
# Last-use / liveness
# ---------------------------------------------------------------------------

def test_last_use_basic_chain():
    dag, plan = _plan(
        {"x0": [8, 8], "w": [8, 8]},
        [
            {"name": "x1", "op": "matmul", "inputs": ["x0", "w"]},
            {"name": "x2", "op": "matmul", "inputs": ["x1", "w"]},
        ],
        ["x2"],
    )
    idx = {n: i for i, n in enumerate(dag.order)}
    # x0 is read only at x1 -> last use = index of x1.
    assert plan.last_use["x0"] == idx["x1"]
    # w is read at x1 and x2 -> last use = index of x2.
    assert plan.last_use["w"] == idx["x2"]
    # Outputs are pinned live to the final execution point.
    assert plan.last_use["x2"] == len(dag.order) - 1


def test_multi_consumer_keeps_buffer_alive_until_last_reader():
    dag, plan = _plan(
        {"a": [2, 3], "w": [3, 4], "w2": [4, 2], "b": [2, 4], "c": [2]},
        [
            {"name": "t1", "op": "matmul", "inputs": ["a", "w"]},
            {"name": "t2", "op": "add", "inputs": ["t1", "b"]},
            {"name": "t3", "op": "matmul", "inputs": ["t1", "w2"]},
            {"name": "t4", "op": "add", "inputs": ["t3", "c"]},
        ],
        ["t2", "t4"],
    )
    b1 = plan.buffer_for("t1")
    # t1's window must still be occupied at t3's birth (second consumer).
    b3 = plan.buffer_for("t3")
    assert b1.last_use >= b3.birth
    assert overlapping_live_pairs(plan) == []


# ---------------------------------------------------------------------------
# Reuse mechanics
# ---------------------------------------------------------------------------

def test_chain_slots_are_reused():
    dag, plan = _plan(
        {"x0": [8, 8], "w": [8, 8]},
        [
            {"name": f"x{i}", "op": "matmul", "inputs": [f"x{i-1}", "w"]}
            for i in range(1, 5)
        ],
        ["x4"],
    )
    # 8x8 tensors = 64 el. w (input, dies at x4), x0 (dies at x1), and a
    # single rotating slot for x1..x4: arena must be ~3*64, not 6*64.
    assert plan.arena_elements == 3 * 64
    offsets = [plan.buffer_for(f"x{i}").offset for i in range(1, 5)]
    assert len(set(offsets)) < len(offsets)  # some offsets repeat
    reuse_events = [e for e in plan.events if e["reused_slot"]]
    assert reuse_events


def test_arena_never_smaller_than_theoretical_peak():
    dag, plan = _plan(
        {"a": [2, 3], "w": [3, 4], "b": [2, 4]},
        [
            {"name": "t1", "op": "matmul", "inputs": ["a", "w"]},
            {"name": "t2", "op": "add", "inputs": ["t1", "b"]},
        ],
        ["t2"],
    )
    assert plan.arena_elements >= plan.theoretical_peak_elements


def test_live_windows_disjoint_for_every_execution_point():
    dag, plan = _plan(
        {"a": [2, 3], "w": [3, 4], "w2": [4, 2], "b": [2, 4], "c": [2]},
        [
            {"name": "t1", "op": "matmul", "inputs": ["a", "w"]},
            {"name": "t2", "op": "add", "inputs": ["t1", "b"]},
            {"name": "t3", "op": "matmul", "inputs": ["t1", "w2"]},
            {"name": "t4", "op": "add", "inputs": ["t3", "c"]},
        ],
        ["t2", "t4"],
    )
    bufs = list(plan.buffers.values())
    by_id = {b.id: b for b in bufs}
    for point, live_ids in enumerate(plan.live_buffers_at):
        windows = sorted(by_id[i].window for i in live_ids)
        for (lo1, hi1), (lo2, hi2) in zip(windows, windows[1:]):
            assert hi1 <= lo2, (point, windows)


# ---------------------------------------------------------------------------
# Slice views / aliasing
# ---------------------------------------------------------------------------

def test_slice_view_owns_no_buffer_and_maps_to_root():
    dag, plan = _plan(
        {"x": [4, 6], "w": [3, 2], "c": [2]},
        [
            {"name": "v", "op": "slice", "inputs": ["x"],
             "starts": [1, 2], "stops": [4, 5]},
            {"name": "t", "op": "matmul", "inputs": ["v", "w"]},
            {"name": "u", "op": "add", "inputs": ["t", "c"]},
        ],
        ["u"],
    )
    assert "v" in plan.views
    assert plan.buffer_of["v"] == plan.buffer_of["x"]
    assert plan.buffer_for("x").members == ("x", "v")


def test_view_offsets_match_flat_numpy_layout():
    import numpy as np

    dag, plan = _plan(
        {"x": [4, 6]},
        [{"name": "v", "op": "slice", "inputs": ["x"],
          "starts": [1, 2], "stops": [4, 5]}],
        ["v"],
    )
    x = np.arange(24, dtype=np.float32).reshape(4, 6)
    v_np = x[1:4, 2:5]
    info = plan.views["v"]
    root_off = plan.buffer_for("x").offset
    arena = np.zeros(plan.arena_elements, dtype=np.float32)
    arena[root_off : root_off + 24] = x.ravel()
    root_view = arena[root_off : root_off + 24].reshape(x.shape)
    rebuilt = root_view[
        tuple(slice(s, e) for s, e in zip((1, 2), (4, 5)))
    ]
    assert rebuilt.strides == v_np.strides
    assert np.array_equal(rebuilt, v_np)
    # The planner's reported relative flat offset points at the same cell.
    assert rebuilt.ravel()[0] == arena[root_off + info.rel_offset]
    assert list(info.shape) == [3, 3]


def test_shared_views_extend_root_lifetime():
    dag, plan = _plan(
        {"X": [4, 6], "W": [3, 2], "bias": [3, 3], "c": [2]},
        [
            {"name": "v", "op": "slice", "inputs": ["X"],
             "starts": [1, 2], "stops": [4, 5]},
            {"name": "v2", "op": "slice", "inputs": ["X"],
             "starts": [0, 0], "stops": [3, 3]},
            {"name": "t1", "op": "matmul", "inputs": ["v", "W"]},
            {"name": "t3", "op": "add", "inputs": ["v", "v2"]},
            {"name": "v3", "op": "slice", "inputs": ["v2"],
             "starts": [0, 0], "stops": [2, 2]},
            {"name": "t4", "op": "add", "inputs": ["t3", "bias"]},
            {"name": "t5", "op": "add", "inputs": ["t1", "c"]},
        ],
        ["t4", "t5", "v3"],
    )
    root_buf = plan.buffer_for("X")
    # Family X,v,v2,v3 must live until v3 (an output -> final point).
    assert root_buf.last_use == len(dag.order) - 1
    assert set(root_buf.members) == {"X", "v", "v2", "v3"}
    assert overlapping_live_pairs(plan) == []


def test_chained_view_global_offsets():
    dag, plan = _plan(
        {"x": [6, 8]},
        [
            {"name": "v", "op": "slice", "inputs": ["x"],
             "starts": [1, 2], "stops": [5, 7]},       # (4,5)
            {"name": "vv", "op": "slice", "inputs": ["v"],
             "starts": [1, 0], "stops": [3, 4]},        # (2,4)
        ],
        ["vv"],
    )
    # Global window in root coords: rows [2:4), cols [2:6).
    info = plan.views["vv"]
    assert (info.min_offset, info.end_offset) == (
        2 * 8 + 2,
        3 * 8 + 6,
    )


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------

def test_budget_enforced_raises():
    with pytest.raises(BudgetExceeded):
        _plan(
            {"x0": [8, 8], "w": [8, 8]},
            [{"name": "x1", "op": "matmul", "inputs": ["x0", "w"]}],
            ["x1"],
            peak_budget_elements=10,
            enforce_budget=True,
        )


def test_budget_report_only_does_not_raise():
    _, plan = _plan(
        {"x0": [8, 8], "w": [8, 8]},
        [{"name": "x1", "op": "matmul", "inputs": ["x0", "w"]}],
        ["x1"],
        peak_budget_elements=10_000,
        enforce_budget=False,
    )
    assert plan.arena_elements <= 10_000


def test_tight_budget_chain_example():
    ops = [
        {"name": f"x{i}", "op": "matmul", "inputs": [f"x{i-1}", "w"]}
        for i in range(1, 9)
    ]
    _, plan = _plan(
        {"x0": [8, 8], "w": [8, 8]},
        ops,
        ["x8"],
        peak_budget_elements=192,
        enforce_budget=True,
    )
    assert plan.arena_elements == 192


def test_zero_size_roots_sharing_offset_are_not_conflicts():
    # Two 0-element roots born while nothing is freed both get fresh slots
    # at the same arena offset; their empty windows never touch, so the
    # planner must accept the graph and both executors must agree.
    dag, plan = _plan(
        {"x": [4, 4], "w": [4, 2], "w2": [4, 3], "a": [2, 2]},
        [
            {"name": "e", "op": "slice", "inputs": ["x"],
             "starts": [0, 0], "stops": [0, 4]},
            {"name": "t0", "op": "matmul", "inputs": ["e", "w"]},
            {"name": "t1", "op": "matmul", "inputs": ["e", "w2"]},
            {"name": "z", "op": "add", "inputs": ["a", "a"]},
        ],
        ["t0", "t1", "z", "w", "w2"],
    )
    assert dag.nodes["t0"].shape == (0, 2)
    assert overlapping_live_pairs(plan) == []
    from tmem.executor import execute

    run = execute(dag, mode="both")
    assert run["comparison"]["match"]
    assert run["results"]["reuse"].outputs["t0"].shape == (0, 2)

def test_num_elements_and_strides():
    assert num_elements((2, 3, 4)) == 24
    assert num_elements(()) == 1
    assert c_strides((2, 3, 4)) == (12, 4, 1)
    assert c_strides((5,)) == (1,)
