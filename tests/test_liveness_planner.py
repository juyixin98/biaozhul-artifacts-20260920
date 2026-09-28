"""Exact small-graph checks for liveness, conflicts and planner placement.

All expected byte numbers are hand-derived from closed-wave intervals and
64-byte aligned records; these tests do not recompute expectations from the
implementation under test.
"""

from __future__ import annotations

import pytest

from tensor_mem import fixtures as fx
from tensor_mem.errors import ResourceExhaustedError
from tensor_mem.liveness import PERSISTENT, analyze_liveness
from tensor_mem.planner import plan_graph

ALIGN = 64
B = 64  # one 4x4 float32 tile, aligned


@pytest.mark.unit
def test_diamond_liveness_intervals_hand_checked():
    g = fx.build_diamond_graph()
    live = analyze_liveness(g, ALIGN)
    by = {r.rid: r for r in live.records}
    assert by["val::x"].first_wave == 0 and by["val::x"].last_wave == 1
    assert by["val::t"].first_wave == 0 and by["val::t"].last_wave == 1
    assert by["val::a"].first_wave == 1 and by["val::a"].last_wave == 2
    assert by["val::m"].first_wave == 1 and by["val::m"].last_wave == 2
    assert by["val::y"].first_wave == 2 and by["val::y"].last_wave is PERSISTENT
    # elementwise diamond has no scratch workspaces
    assert all(r.kind == "value" for r in live.records)


@pytest.mark.unit
def test_diamond_plan_peak_and_reuse_exact():
    g = fx.build_diamond_graph()
    plan = plan_graph(g, alignment=ALIGN)
    assert plan.conflicts == []
    # Hand-derived placement: t and persistent y share slot0; x slot1;
    # the two parallel branches a, m get separate slots slot2/slot3.
    assert plan.slot_of("val::t") == plan.slot_of("val::y")
    assert plan.slot_of("val::a") != plan.slot_of("val::m")
    assert plan.peak_capacity == 4 * B          # 256 at wave 1
    assert plan.peak_resident == 4 * B
    assert plan.persistent_bytes == B
    assert plan.no_reuse_total == 5 * B          # 320 without any reuse


@pytest.mark.unit
def test_parallel_branches_conflict_is_classified():
    g = fx.build_diamond_graph()
    live = analyze_liveness(g, ALIGN)
    conflicts = live.all_pairwise_conflicts(g)
    by_pair = {
        tuple(sorted((c.record_a, c.record_b))): c.reason for c in conflicts
    }
    assert by_pair[(f"val::a", f"val::m")] == "parallel_branch"
    # t is read by both branch nodes during the 2-node wave: concurrent overlap
    assert by_pair[("val::a", f"val::t")] == "concurrent_wave"
    assert by_pair[("val::m", f"val::t")] == "concurrent_wave"
    # the retained join output overlaps branch outputs at the join wave
    assert by_pair[("val::a", "val::y")] == "retained_output_live"
    assert by_pair[("val::m", "val::y")] == "retained_output_live"


@pytest.mark.unit
def test_validator_detects_forced_parallel_slot_share():
    g = fx.build_diamond_graph()
    live = analyze_liveness(g, ALIGN)
    # Tamper with a correct plan: force the two parallel outputs into one slot.
    plan = plan_graph(g, alignment=ALIGN)
    bad = dict(plan.assignment)
    bad["val::m"] = bad["val::a"]
    violations = live.validate_assignment(bad, g)
    reasons = {(v.record_a, v.record_b, v.reason) for v in violations}
    assert any(
        {a, b} == {"val::a", "val::m"} and reason == "parallel_branch"
        for a, b, reason in reasons
    )


@pytest.mark.unit
def test_long_lived_output_pins_storage_and_chain_reuses_rest():
    g = fx.build_long_lived_graph()
    plan = plan_graph(g, alignment=ALIGN)
    assert plan.conflicts == []
    early_slot = plan.slot_of("val::early")
    # x dies after wave 0; late1 takes its slot; late2 overlaps late1 at wave
    # 2 so gets its own slot; final (wave 3) returns to the x/late1 slot.
    first_slot = plan.slot_of("val::x")
    assert plan.slot_of("val::late1") == first_slot
    late2_slot = plan.slot_of("val::late2")
    assert late2_slot != first_slot
    assert plan.slot_of("val::final") == first_slot
    assert early_slot not in {first_slot, late2_slot}
    # wave barrier model: wave 2 holds early + late1 + late2 = 3 slots; the
    # persistent end state holds only the two retained outputs.
    assert plan.peak_capacity == 3 * B
    assert plan.persistent_bytes == 2 * B       # early and final both retained
    assert plan.total_pool_bytes == 3 * B       # early, chain slot, late2 slot
    assert plan.no_reuse_total == 5 * B


@pytest.mark.unit
def test_retained_output_conflict_detected_on_forced_share():
    g = fx.build_long_lived_graph()
    live = analyze_liveness(g, ALIGN)
    plan = plan_graph(g, alignment=ALIGN)
    bad = dict(plan.assignment)
    bad["val::final"] = bad["val::early"]  # early is retained client output
    violations = live.validate_assignment(bad, g)
    assert any(
        {v.record_a, v.record_b} == {"val::early", "val::final"}
        and v.reason == "retained_output_live"
        for v in violations
    )


@pytest.mark.unit
def test_workspace_bytes_count_at_peak_and_reuse_across_waves():
    g = fx.build_workspace_graph()
    plan = plan_graph(g, alignment=ALIGN)
    assert plan.conflicts == []
    # scratch for both GEMMs is one slot reused across the two waves
    assert plan.slot_of("ws::n_mm1") == plan.slot_of("ws::n_mm2")
    # hand-derived peak at wave 0: a=128 plus four 64-byte slots
    # (b, c, t, ws_mm1) = 384
    assert plan.peak_capacity == 384
    assert plan.peak_resident == 384
    # the two live workspaces are never placed together (different waves)
    w0 = plan.wave_stats[0]
    w1 = plan.wave_stats[1]
    assert "ws::n_mm1" in w0.live_records
    assert "ws::n_mm2" not in w0.live_records
    assert "ws::n_mm2" in w1.live_records
    assert plan.no_reuse_total == 512          # 128 + 6*64


@pytest.mark.unit
def test_concurrent_branches_get_disjoint_slots_all_retained():
    g = fx.build_concurrent_branches_graph(3)
    live = analyze_liveness(g, ALIGN)
    plan = plan_graph(g, alignment=ALIGN)
    assert plan.conflicts == []
    branch_slots = {plan.slot_of(f"val::o{i}") for i in range(3)}
    ws_slots = {plan.slot_of(f"ws::n_branch_{i}") for i in range(3)}
    assert len(branch_slots) == 3
    assert len(ws_slots) == 3
    assert branch_slots.isdisjoint(ws_slots)
    # 4 feeds + 3 outputs + 3 workspaces live simultaneously at wave 0
    assert plan.peak_capacity == 10 * B        # 640
    assert plan.persistent_bytes == 3 * B
    # raw conflict analysis flags every same-wave output pair
    pairs = [
        c for c in live.all_pairwise_conflicts(g)
        if c.reason == "parallel_branch"
    ]
    assert len(pairs) == 3                     # C(3, 2)


@pytest.mark.unit
def test_aliasing_merges_class_and_storage():
    g = fx.build_alias_graph()
    live = analyze_liveness(g, ALIGN)
    root = live.alias_root
    assert root["r"] == root["v"] == root["z"]
    plan = plan_graph(g, alignment=ALIGN)
    rid = f"val::{root['r']}"
    rec = plan.records[rid]
    assert set(rec.members) == {"r", "v", "z"}
    # alias class occupies exactly one slot, retained because r,z are outputs
    assert sum(1 for s in plan.slots.values() if rid in s.assigned) == 1
    assert plan.records[rid].last_wave is PERSISTENT
    assert plan.peak_capacity == 2 * B


@pytest.mark.unit
def test_no_reuse_plan_allocates_every_record_separately():
    g = fx.build_diamond_graph()
    reused = plan_graph(g, alignment=ALIGN, allow_reuse=True)
    naive = plan_graph(g, alignment=ALIGN, allow_reuse=False)
    assert len(naive.slots) == len(naive.records)
    assert len(naive.slots) > len(reused.slots)
    # Under wave barriers the per-wave high-water mark is reuse-invariant;
    # what reuse cuts is the total arena reservation (one slot per record vs
    # one slot per disjoint interval).
    assert naive.total_pool_bytes == naive.no_reuse_total
    assert reused.total_pool_bytes < naive.total_pool_bytes
    assert reused.peak_resident <= naive.peak_resident


@pytest.mark.unit
def test_budget_exceeded_reports_category_and_exact_overage():
    g = fx.build_concurrent_branches_graph(3)
    with pytest.raises(ResourceExhaustedError) as exc:
        plan_graph(g, budget=600, alignment=ALIGN)
    err = exc.value
    assert err.category == "resource_exhausted"
    assert err.details["peak_resident_bytes"] == 640
    assert err.details["budget_bytes"] == 600
    assert err.details["over_bytes"] == 40
    assert err.details["wave_peak"] == 0


@pytest.mark.unit
def test_budget_at_exact_peak_is_accepted():
    g = fx.build_diamond_graph()
    plan = plan_graph(g, budget=256, alignment=ALIGN)
    assert plan.peak_resident == 256


@pytest.mark.unit
def test_external_state_storage_counts_but_is_not_pool_placed():
    g = fx.build_linear_grad_graph()
    plan = plan_graph(
        g, alignment=ALIGN, external_names=frozenset({"W"})
    )
    assert "val::W" not in plan.assignment
    w_rec = plan.records["val::W"]
    assert w_rec.external is True
    # W (64B) resident at wave 0 alongside the pool
    assert plan.wave_stats[0].external_bytes == 64
    assert plan.wave_stats[0].resident_bytes == (
        plan.wave_stats[0].live_capacity + 64
    )
    assert plan.peak_resident == 320
    assert plan.peak_capacity == 320          # wave 1 pool-only peak also 320
