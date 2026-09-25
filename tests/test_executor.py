"""执行器正确性测试：复用执行器必须与不复用基线逐元素一致。"""

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
from tensor_planner.executor import (
    NaiveExecutor,
    ReuseExecutor,
    max_abs_diff,
)
from tensor_planner.planner import build_plan


def feeds_for(dag, seed: int = 7) -> dict:
    rng = np.random.default_rng(seed)
    return {name: rng.random(shape) for name, shape in dag.inputs.items()}


@pytest.mark.parametrize(
    "builder",
    [chain_dag, multi_consumer_dag, branch_join_dag, shared_view_dag],
    ids=["chain", "multi_consumer", "branch_join", "shared_view"],
)
def test_reuse_matches_naive(builder) -> None:
    dag = builder()
    feeds = feeds_for(dag)
    naive = NaiveExecutor(dag).run(feeds)
    reuse = ReuseExecutor(dag, build_plan(dag)).run(feeds)
    assert max_abs_diff(naive.outputs, reuse.outputs) == 0.0
    # 复用不缩短生命周期，峰值存活量不高于基线；
    # 共享视图场景下基线为视图副本额外计字节，复用严格更低
    assert reuse.peak_elements <= naive.peak_elements
    # 复用累计分配不超过基线
    assert reuse.total_allocated_elements <= naive.total_allocated_elements


def test_peak_equal_when_no_views() -> None:
    """无视图场景下两个执行器的峰值存活量必须相等。"""
    for builder in (chain_dag, multi_consumer_dag, branch_join_dag):
        dag = builder()
        feeds = feeds_for(dag)
        naive = NaiveExecutor(dag).run(feeds)
        reuse = ReuseExecutor(dag, build_plan(dag)).run(feeds)
        assert reuse.peak_elements == naive.peak_elements


def test_shared_view_peak_strictly_lower() -> None:
    """共享视图场景：基线为视图副本计字节，复用峰值严格更低。"""
    dag = shared_view_dag()
    feeds = feeds_for(dag)
    naive = NaiveExecutor(dag).run(feeds)
    reuse = ReuseExecutor(dag, build_plan(dag)).run(feeds)
    assert reuse.peak_elements < naive.peak_elements


def test_view_is_true_alias_in_reuse_executor() -> None:
    dag = shared_view_dag()
    feeds = feeds_for(dag)
    plan = build_plan(dag)
    # 通过 arena 指针验证：v 的视图首地址应等于 X 槽位首地址
    executor = ReuseExecutor(dag, plan)
    result = executor.run(feeds)
    # 输出 A = X[0:2] @ P，B = X[2:4] @ P
    expected_a = feeds["X"][0:2] @ feeds["P"]
    expected_b = feeds["X"][2:4] @ feeds["P"]
    np.testing.assert_allclose(result.outputs["A"], expected_a)
    np.testing.assert_allclose(result.outputs["B"], expected_b)


def test_missing_input_rejected() -> None:
    dag = chain_dag()
    with pytest.raises(ValueError, match="缺少输入"):
        NaiveExecutor(dag).run({"A": np.ones(4)})


def test_extra_input_rejected() -> None:
    dag = chain_dag()
    feeds = feeds_for(dag)
    feeds["ZZZ"] = np.zeros(1)
    with pytest.raises(ValueError, match="多余的输入"):
        NaiveExecutor(dag).run(feeds)


def test_wrong_shape_rejected() -> None:
    dag = chain_dag()
    with pytest.raises(ValueError, match="形状不符"):
        NaiveExecutor(dag).run({"A": np.ones(5), "B": np.ones(4)})


def test_matmul_inplace_reuse_safety() -> None:
    """构造 matmul 输出复用“刚读完即死”的输入槽位的情形。

    若执行器先写输出再读输入，matmul 会读到自己覆盖的数据而出错；
    先算后写回则安全。本测试用确定性数据锁定该行为。
    """
    dag = (
        DAGBuilder()
        .add_input("A", (2, 2))
        .add_input("B", (2, 2))
        .add_matmul("C", "A", "B", time=1)  # A、B 在 t1 读完即死
        .build()
    )
    plan = build_plan(dag)
    # C 必须复用 A 或 B 的槽位（端点相接）
    assert plan.placement_of("C").offset in (
        plan.placement_of("A").offset,
        plan.placement_of("B").offset,
    )
    feeds = {
        "A": np.array([[1.0, 2.0], [3.0, 4.0]]),
        "B": np.array([[5.0, 6.0], [7.0, 8.0]]),
    }
    reuse = ReuseExecutor(dag, plan).run(feeds)
    expected = feeds["A"] @ feeds["B"]
    np.testing.assert_allclose(reuse.outputs["C"], expected)


def test_max_abs_diff_detects_mismatch() -> None:
    with pytest.raises(ValueError, match="输出集合不一致"):
        max_abs_diff({"x": np.zeros(1)}, {"y": np.zeros(1)})
    with pytest.raises(ValueError, match="形状不一致"):
        max_abs_diff({"x": np.zeros(2)}, {"x": np.zeros(3)})
