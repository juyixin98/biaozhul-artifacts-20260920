"""验收测试：活跃张量不重叠、峰值预算、与基线结果一致。"""

from __future__ import annotations

import numpy as np
import pytest

from graphs import (
    branch_join_dag,
    chain_dag,
    multi_consumer_dag,
    shared_view_dag,
)
from tensor_planner.dag import DAGBuilder
from tensor_planner.executor import ITEMSIZE
from tensor_planner.planner import build_plan
from tensor_planner.verifier import (
    check_bounds,
    check_no_overlap,
    check_views_alias_within_base,
    verify_plan,
)


def feeds_for(dag, seed: int = 3) -> dict:
    rng = np.random.default_rng(seed)
    return {name: rng.random(shape) for name, shape in dag.inputs.items()}


@pytest.mark.parametrize(
    "builder",
    [chain_dag, multi_consumer_dag, branch_join_dag, shared_view_dag],
    ids=["chain", "multi_consumer", "branch_join", "shared_view"],
)
def test_verify_plan_passes(builder) -> None:
    dag = builder()
    v = verify_plan(dag, feeds_for(dag))
    assert v.passed, v.errors
    assert v.max_abs_diff <= 1e-12
    assert v.overlap_violations == []
    assert v.out_of_bounds == []
    assert v.alias_errors == []


def test_budget_within_passes() -> None:
    dag = chain_dag()
    plan = build_plan(dag)
    v = verify_plan(
        dag, feeds_for(dag), budget_elements=plan.arena_size
    )
    assert v.passed
    assert v.within_budget


def test_budget_too_small_fails() -> None:
    dag = chain_dag()
    plan = build_plan(dag)
    v = verify_plan(
        dag, feeds_for(dag), budget_elements=plan.arena_size - 1
    )
    assert not v.passed
    assert not v.within_budget
    assert any("预算" in e for e in v.errors)


def test_overlap_checker_catches_artificial_conflict() -> None:
    """人为构造一个非法 plan（两个同时存活的组落在同一槽位），
    验证 check_no_overlap 确实能发现冲突——证明检查不是恒真。"""
    dag = multi_consumer_dag()
    plan = build_plan(dag)
    slots = dict(plan.placements)
    # A 与 B 在 t=1 同时存活，把 B 挪到 A 的槽位
    a = slots["A"]
    b = slots["B"]
    bad_b = type(b)(
        root=b.root, offset=a.offset, size=b.size, interval=b.interval,
        members=b.members,
    )
    slots["B"] = bad_b
    bad_plan = type(plan)(
        arena_size=plan.arena_size,
        placements=slots,
        storage_intervals=plan.storage_intervals,
        reuse_chains=plan.reuse_chains,
        live_per_time=plan.live_per_time,
        peak_live_elements=plan.peak_live_elements,
        peak_time=plan.peak_time,
        root_of_value=plan.root_of_value,
    )
    violations = check_no_overlap(bad_plan)
    assert len(violations) == 1
    assert {violations[0].root_a, violations[0].root_b} == {"A", "B"}


def test_bounds_checker_catches_out_of_arena() -> None:
    dag = chain_dag()
    plan = build_plan(dag)
    slots = dict(plan.placements)
    a = slots["A"]
    slots["A"] = type(a)(
        root=a.root, offset=plan.arena_size, size=a.size,
        interval=a.interval, members=a.members,
    )
    bad_plan = type(plan)(
        arena_size=plan.arena_size,
        placements=slots,
        storage_intervals=plan.storage_intervals,
        reuse_chains=plan.reuse_chains,
        live_per_time=plan.live_per_time,
        peak_live_elements=plan.peak_live_elements,
        peak_time=plan.peak_time,
        root_of_value=plan.root_of_value,
    )
    errors = check_bounds(bad_plan)
    assert len(errors) == 1


def test_alias_check_on_shared_view() -> None:
    dag = shared_view_dag()
    plan = build_plan(dag)
    assert check_views_alias_within_base(dag, plan) == []


def test_memory_report_fields() -> None:
    dag = shared_view_dag()
    v = verify_plan(dag, feeds_for(dag))
    assert v.passed
    assert v.arena_bytes == v.arena_elements * ITEMSIZE
    # 基线累计分配 56 元素，复用 arena 34 元素
    assert v.naive_total_allocated == 56
    assert v.reuse_total_allocated == 34
    assert v.saved_total_elements == 22
    assert v.summary()  # 可生成摘要文本


def test_deep_chain_savings_scale() -> None:
    """长链：n 个中间值，复用后 arena 应恒为 2 个槽位。"""
    n = 12
    b = DAGBuilder().add_input("x0", (5,)).add_input("w", (5,))
    prev = "x0"
    for i in range(1, n + 1):
        b.add_add(f"x{i}", prev, "w", time=i)
        prev = f"x{i}"
    dag = b.build()
    v = verify_plan(dag, feeds_for(dag))
    assert v.passed
    assert v.arena_elements == 10  # 永远只需两个 5 元素槽位
    assert v.naive_total_allocated == 5 * (n + 2)
