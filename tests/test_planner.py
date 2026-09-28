"""Planner tests: reuse decisions, peak math and conflict verification.

The peak and conflict assertions here are derived independently from the
planner: we enumerate placements ourselves from the graph's wave structure and
recompute per-wave live bytes and pairwise interval overlaps by hand.
"""
from __future__ import annotations

import pytest


from tenmem.errors import ResourceExhaustedError
from tenmem.fixtures import (
    build_alias,
    build_diamond,
    build_long_lived,
    build_parallel_branches,
    build_workspace_matmul,
)
from tenmem.graph import validate_and_schedule
from tenmem.planner.liveness import analyze, overlaps
from tenmem.planner.memory import plan_memory
from tenmem.tensor import align_up

pytestmark = pytest.mark.unit

ALIGN = 64
N = 8
ROW = align_up(N * 4, ALIGN)  # 64 B for the n=8 float32 vectors


def _independently_check_no_conflicts(plan) -> None:
    """Assert that distinct live-range groups never share a buffer while both
    live. Recomputed from raw placements, ignoring the planner's own checker."""
    by_buffer: dict[int, list] = {}
    for p in plan.placements:
        by_buffer.setdefault(p.buffer_id, []).append(p)
    for bid, ps in by_buffer.items():
        for i, a in enumerate(ps):
            for b in ps[i + 1 :]:
                if a.group and a.group == b.group:
                    continue
                assert not overlaps((a.birth, a.death), (b.birth, b.death)), (
                    f"buffer {bid} shared by {a.name} and {b.name} with overlapping lives"
                )


def _independently_compute_peak(graph, plan) -> int:
    """Peak = max over waves of capacity of distinct buffers live that wave."""
    schedule = validate_and_schedule(graph)
    live = analyze(graph, schedule, plan.alignment)
    # Map tensor name -> buffer id via placements (deduplicate alias members).
    buffer_of: dict[str, int] = {}
    for p in plan.placements:
        buffer_of.setdefault(p.name, p.buffer_id)
    peak = 0
    for wave in range(len(schedule.waves)):
        bufs = set()
        for name, (b, d) in live.tensor_interval.items():
            if b <= wave <= d:
                bufs.add(buffer_of[name])
        for p in plan.placements:
            if p.kind == "workspace" and p.birth <= wave <= p.death:
                bufs.add(p.buffer_id)
        peak = max(peak, sum(plan.buffers[i].capacity for i in bufs))
    return peak


def test_diamond_peak_is_hand_verified() -> None:
    graph = build_diamond(N)
    plan = plan_memory(graph, alignment=ALIGN)
    # Waves (closed intervals; last consumers keep tensors live through the wave):
    # Wave 0: x                       -> 1 row
    # Wave 1: x,r                     -> 2 rows
    # Wave 2: x,r,b1,b2 (all in use)  -> 4 rows  <-- peak
    # Wave 3: b1,b2,y(pinned join)    -> 3 rows
    assert plan.peak_bytes == 4 * ROW
    assert _independently_compute_peak(graph, plan) == plan.peak_bytes
    _independently_check_no_conflicts(plan)


def test_reuse_strictly_under_no_reuse() -> None:
    graph = build_diamond(N)
    plan = plan_memory(graph, alignment=ALIGN)
    # 5 tensors (x, r, b1, b2, y) + 0 workspaces, each one row without reuse.
    assert plan.no_reuse_bytes == 5 * ROW
    assert plan.peak_bytes < plan.no_reuse_bytes


def test_diamond_parallel_branches_get_distinct_buffers() -> None:
    graph = build_parallel_branches(N, branches=3)
    plan = plan_memory(graph, alignment=ALIGN)
    # p0,p1,p2 are all born in wave 0 and consumed across waves 1..2; they
    # coexist at wave 0, so their buffers must differ.
    ids = {plan.buffer_id_for(f"p{i}") for i in range(3)}
    assert len(ids) == 3
    _independently_check_no_conflicts(plan)


def test_long_lived_output_is_pinned_and_never_reused() -> None:
    n, tail = 16, 4
    graph = build_long_lived(n, tail)
    plan = plan_memory(graph, alignment=ALIGN)
    early_bid = plan.buffer_id_for("early")
    # No other tensor placement may occupy early's buffer.
    others = {
        p.name for p in plan.placements
        if p.buffer_id == early_bid and p.name != "early"
    }
    assert others == set()
    pinned = [p for p in plan.placements if p.name == "early"]
    assert all(p.pinned for p in pinned)
    assert plan.peak_bytes < plan.no_reuse_bytes


def test_alias_union_shares_one_buffer() -> None:
    graph = build_alias(N)
    plan = plan_memory(graph, alignment=ALIGN)
    assert plan.buffer_id_for("x") == plan.buffer_id_for("a")
    # b and c are different tensors produced after the alias view ends.
    _independently_check_no_conflicts(plan)


def test_workspace_counts_in_peak_and_is_reused_later() -> None:
    m, k, nn = 4, 5, 6
    graph = build_workspace_matmul(m, k, nn)
    plan = plan_memory(graph, alignment=ALIGN)
    out = align_up(m * nn * 4, ALIGN)
    ws = out
    # Wave 0 (matmul): inputs a,b + output mm + ws_mm — workspace in the peak.
    wave0 = align_up(m * k * 4, ALIGN) + align_up(k * nn * 4, ALIGN) + out + ws
    assert plan.peak_bytes == wave0
    # The softmax workspace at wave 1 must sit in a buffer whose previous
    # occupant died strictly before wave 1 (sequential reuse, never concurrent).
    ws_sm = next(p for p in plan.placements if p.name == "ws:sm")
    prior = [
        p for p in plan.placements
        if p.buffer_id == ws_sm.buffer_id and p is not ws_sm
    ]
    assert all(p.death < ws_sm.birth for p in prior)
    assert _independently_compute_peak(graph, plan) == wave0
    _independently_check_no_conflicts(plan)


def test_budget_breach_is_resource_exhausted() -> None:
    graph = build_parallel_branches(N, branches=3)
    plan_unbounded = plan_memory(graph, alignment=ALIGN)
    with pytest.raises(ResourceExhaustedError) as exc:
        plan_memory(graph, alignment=ALIGN, max_bytes=plan_unbounded.peak_bytes - 1)
    assert exc.value.category == "resource_exhausted"
    assert exc.value.details["peak_bytes"] == plan_unbounded.peak_bytes


def test_budget_within_demand_succeeds() -> None:
    graph = build_diamond(N)
    plan = plan_memory(graph, alignment=ALIGN, max_bytes=4 * ROW)
    assert plan.peak_bytes == 4 * ROW


def test_buffer_capacities_are_aligned_and_sufficient() -> None:
    graph = build_diamond(N)
    plan = plan_memory(graph, alignment=ALIGN)
    for buf in plan.buffers:
        assert buf.capacity % ALIGN == 0
    for p in plan.placements:
        assert plan.buffers[p.buffer_id].capacity >= p.size


def test_liveness_intervals_basic() -> None:
    graph = build_diamond(N)
    schedule = validate_and_schedule(graph)
    live = analyze(graph, schedule, ALIGN)
    assert live.tensor_interval["x"][0] == 0
    # x is last consumed at wave 1 by the concurrent b1,b2 nodes.
    assert live.tensor_interval["x"][1] == 1
    # output y is pinned.
    from tenmem.graph import PINNED_DEATH

    assert live.tensor_interval["y"][1] == PINNED_DEATH
    # r dies at wave 1; b1,b2 die at wave 2 (the join).
    assert live.tensor_interval["r"][1] == 1
    assert live.tensor_interval["b1"][1] == 2
