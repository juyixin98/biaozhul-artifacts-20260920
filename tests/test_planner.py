"""生命周期分析与复用规划器测试。"""

from __future__ import annotations

from graphs import (
    branch_join_dag,
    chain_dag,
    multi_consumer_dag,
    shared_view_dag,
)
from tensor_planner.liveness import (
    LiveInterval,
    storage_intervals,
    value_intervals,
)
from tensor_planner.planner import (
    build_plan,
    naive_total_elements,
)
from tensor_planner.verifier import check_no_overlap


def test_value_intervals_basic() -> None:
    dag = chain_dag()
    iv = value_intervals(dag)
    assert iv["A"] == LiveInterval(0, 1)  # time=1 读完即死
    assert iv["B"] == LiveInterval(0, 2)  # 被 t1、t2 两次读取
    assert iv["C"] == LiveInterval(1, 2)
    assert iv["D"] == LiveInterval(2, 3)  # 输出存活到 horizon=3


def test_endpoint_touch_is_not_overlap() -> None:
    assert LiveInterval(0, 1).overlaps(LiveInterval(1, 2)) is False
    assert LiveInterval(0, 2).overlaps(LiveInterval(1, 3)) is True


def test_plan_reuses_dead_slots() -> None:
    dag = chain_dag()
    plan = build_plan(dag)
    # A 与 C 生命周期端点相接，必须复用同一槽位
    assert plan.placement_of("A").offset == plan.placement_of("C").offset
    # t2 时 B、C 都死，D 可复用合并后的空闲段
    assert plan.placement_of("D").offset == plan.placement_of("A").offset
    chains = dict(plan.reuse_chains)
    assert plan.placement_of("A").offset in chains
    assert plan.arena_size == 8  # 只需两个 4 元素槽位
    assert naive_total_elements(dag) == 16  # 基线累计申请 4 个值


def test_no_overlapping_live_slots() -> None:
    dag = chain_dag()
    plan = build_plan(dag)
    assert check_no_overlap(plan) == []
    assert plan.arena_size >= plan.peak_live_elements



def test_multi_consumer_extends_liveness() -> None:
    dag = multi_consumer_dag()
    iv = value_intervals(dag)
    assert iv["A"] == LiveInterval(0, 3)
    plan = build_plan(dag)
    assert check_no_overlap(plan) == []
    # C 在 t1 出生时 A 仍存活，无法复用 A，只能 bump 新槽位
    assert plan.placement_of("C").offset != plan.placement_of("A").offset
    # 三个 8 元素槽位并存（A、B、C），峰值 24
    assert plan.peak_live_elements == 24
    assert plan.arena_size == 24
    # E 在 t3 出生时 A、D 均死，可回收
    assert plan.placement_of("E").offset in (
        plan.placement_of("A").offset,
        plan.placement_of("D").offset,
    )



def test_branch_join_intervals_and_plan() -> None:
    dag = branch_join_dag()
    iv = value_intervals(dag)
    assert iv["P"] == LiveInterval(0, 2)  # 多消费者
    assert iv["Q"] == LiveInterval(0, 1)
    assert iv["R"] == LiveInterval(0, 2)
    assert iv["A"] == LiveInterval(1, 3)
    assert iv["B"] == LiveInterval(2, 3)
    assert iv["C"] == LiveInterval(3, 4)
    plan = build_plan(dag)
    assert check_no_overlap(plan) == []
    # A 复用 Q 死后的槽位
    assert plan.placement_of("A").offset == plan.placement_of("Q").offset
    assert plan.arena_size == 18  # t1 时 P、Q、R、A 四个 6 元素槽位并存
    # 逐时刻物理存活（槽位、半开区间）
    per = plan.live_per_time
    assert per[0] == 18
    assert per[1] == 18
    assert per[2] == 12
    assert per[3] == 6



def test_shared_view_storage_group() -> None:
    dag = shared_view_dag()
    stor = storage_intervals(dag)
    # 视图并入基底组，区间外包到最后一个视图的死亡时刻 (w -> 4)
    assert set(stor) == {"X", "P", "A", "B"}
    assert stor["X"] == LiveInterval(0, 4)
    plan = build_plan(dag)
    # 视图没有独立槽位，直接解析到基底
    assert plan.placement_of("v").root == "X"
    assert plan.placement_of("w").root == "X"
    assert check_no_overlap(plan) == []
    # 基线把 v、w 各复制一份：16+12+8+8+6+6=56；复用 arena=34
    assert naive_total_elements(dag) == 56
    assert plan.arena_size == 34


def test_peak_never_exceeds_arena() -> None:
    for builder in (chain_dag, multi_consumer_dag, branch_join_dag, shared_view_dag):
        plan = build_plan(builder())
        assert plan.peak_live_elements <= plan.arena_size
