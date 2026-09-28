"""执行引擎行为测试（第二阶段要求的核心）。

覆盖：
- RNG 状态重放 + 不重复外部副作用；
- 内存预算同时包含保留激活与临时工作区；
- 共享子图引用计数正确；
- 额外计算量真实、梯度与基线/独立有限差分一致；
- 静态预测峰值 == 运行时高水位（对账）。
"""

from __future__ import annotations

import numpy as np
import pytest

from recomp_scheduler.engine import execute
from recomp_scheduler.errors import (
    BudgetInfeasibleError,
    ComputeFailureError,
    StateConflictError,
)
from recomp_scheduler.memory import simulate
from recomp_scheduler.planner import baseline_plan, legal_plans, make_plan
from recomp_scheduler.state import TrainState


def _run(graph, cuts, seed=99, **kw):
    plan = make_plan(cuts, len(graph.order))
    plan.validate(graph)
    state = TrainState.synthetic(graph, seed=seed)
    return execute(state, plan, predicted_peak=simulate(graph, plan).peak_elements, **kw)


def _baseline(graph, seed=99):
    plan, prof = baseline_plan(graph)
    return execute(TrainState.synthetic(graph, seed=seed), plan, predicted_peak=prof.peak_elements)


# --------------------------------------------------------------------- #
# RNG 重放与外部副作用
# --------------------------------------------------------------------- #
def test_rng_replay_reproduces_dropout_mask_exactly(cross_graph) -> None:
    # 方案 (2,5)：块0=[x,l1,r1]，块1=[d1,a1,a2]，块2=[b1,m,l2]。
    # dropout 节点 d1 位于检查点块1内部，必须在反向时从 RNG 快照重放。
    plan = make_plan((2, 5), len(cross_graph.order))
    prof = simulate(cross_graph, plan)
    result = execute(
        TrainState.synthetic(cross_graph, seed=99), plan, predicted_peak=prof.peak_elements
    )
    # d1 的掩码恰好被重放校验一次。
    assert result.effects_emitted == 1
    assert result.effects_replay_verified == 1


def test_no_external_side_effect_on_recompute_boundary(branch_graph) -> None:
    # 方案 (3,)：d1 是边界节点（不重放），因此重放校验数必须为 0，
    # 但前向仍恰好发放一次——副作用既不缺失也不重复。
    plan = make_plan((3,), len(branch_graph.order))
    prof = simulate(branch_graph, plan)
    result = execute(
        TrainState.synthetic(branch_graph, seed=99), plan, predicted_peak=prof.peak_elements
    )
    assert result.effects_emitted == 1
    assert result.effects_replay_verified == 0


def test_snapshot_take_restore_balanced(cross_graph) -> None:
    plan = make_plan((2, 5), len(cross_graph.order))
    prof = simulate(cross_graph, plan)
    result = execute(
        TrainState.synthetic(cross_graph, seed=99), plan, predicted_peak=prof.peak_elements
    )
    # 两个检查点块（0、1）各拍一张、各恢复一张。
    assert result.rng_snapshot_balance == {"taken": 2, "restored": 2}


def test_corrupted_rng_replay_is_compute_failure(monkeypatch, chain_graph) -> None:
    # 让重放阶段的 RNG 抽取结果与前向不同：必须被识别为 compute_failure，
    # 而不是静默产生错误梯度。
    from recomp_scheduler import ops as ops_mod

    real_emit = ops_mod.EffectLog.observe
    calls = {"n": 0}

    def tampered(self, *, kind, node, payload, mode):
        if mode == "recompute":
            calls["n"] += 1
            payload = b"tampered" + payload[8:] if len(payload) > 8 else b"xx"
        return real_emit(self, kind=kind, node=node, payload=payload, mode=mode)

    monkeypatch.setattr(ops_mod.EffectLog, "observe", tampered)
    plan = make_plan((2,), len(chain_graph.order))  # d1(位置3)在检查点块0内? 块0=x..r1
    # 注：(2,) 时 dropout d1(位置3) 位于最后一块，不重放；改用 6 节点图。
    raw = [
        {"id": "x", "op": "input", "attrs": {"shape": [4, 2]}},
        {"id": "l1", "op": "linear", "inputs": ["x"], "attrs": {"out_features": 3}},
        {"id": "r1", "op": "relu", "inputs": ["l1"]},
        {"id": "d1", "op": "dropout", "inputs": ["r1"], "attrs": {"keep_prob": 0.5}},
        {"id": "t1", "op": "tanh", "inputs": ["d1"]},
        {"id": "l2", "op": "linear", "inputs": ["t1"], "attrs": {"out_features": 1}},
    ]
    from recomp_scheduler.graph import build_graph

    g = build_graph(raw, outputs=["l2"])
    plan = make_plan((2, 4), len(g.order))  # d1(3) 在检查点块1内部
    with pytest.raises(ComputeFailureError) as exc:
        execute(
            TrainState.synthetic(g, seed=7),
            plan,
            predicted_peak=simulate(g, plan).peak_elements,
        )
    assert exc.value.category == "compute_failure"
    assert "differs from forward" in exc.value.message
    assert calls["n"] >= 1


# --------------------------------------------------------------------- #
# 内存预算（保留激活 + 工作区）
# --------------------------------------------------------------------- #
def test_runtime_budget_below_predicted_peak_fails(cross_graph) -> None:
    plan = make_plan((2, 5), len(cross_graph.order))
    prof = simulate(cross_graph, plan)
    # 运行时预算低于该方案静态峰值：必须在分配过程中被硬拦截。
    with pytest.raises(BudgetInfeasibleError) as exc:
        execute(
            TrainState.synthetic(cross_graph, seed=99),
            plan,
            predicted_peak=prof.peak_elements,
            budget_elements=prof.peak_elements - 1,
        )
    err = exc.value
    assert err.category == "resource_exhausted"
    assert err.details["phase"] == "runtime"


def test_runtime_budget_equal_peak_succeeds(cross_graph) -> None:
    plan = make_plan((2, 5), len(cross_graph.order))
    prof = simulate(cross_graph, plan)
    result = execute(
        TrainState.synthetic(cross_graph, seed=99),
        plan,
        predicted_peak=prof.peak_elements,
        budget_elements=prof.peak_elements,
    )
    assert result.peak_match()


def test_budget_includes_temporary_workspace(tiny_graph) -> None:
    # 若预算只看保留激活而忽略工作区，将错误地放行一个过低预算。
    # tiny 图上"含工作区峰值"78 与"仅存活峰值"70 之差正是工作区贡献。
    plan, prof = baseline_plan(tiny_graph)
    assert prof.peak_live_without_workspace == 70
    assert prof.peak_elements == 78
    with pytest.raises(BudgetInfeasibleError) as exc:
        execute(
            TrainState.synthetic(tiny_graph, seed=1),
            plan,
            predicted_peak=prof.peak_elements,
            budget_elements=prof.peak_live_without_workspace,
        )
    assert exc.value.details["phase"] == "runtime"


# --------------------------------------------------------------------- #
# 共享子图引用计数
# --------------------------------------------------------------------- #
def test_shared_boundary_reference_count(cross_graph) -> None:
    plan = make_plan((2, 5), len(cross_graph.order))
    prof = simulate(cross_graph, plan)
    result = execute(
        TrainState.synthetic(cross_graph, seed=99), plan, predicted_peak=prof.peak_elements
    )
    # r1(边界) 被块1(经d1..)与块2(经b1)两个后续块消费：初始1 + 2 retain = 3。
    exits = [e for e in result.block_events if e.get("phase") == "forward_exit"]
    r1 = next(e for e in exits if e["boundary"] == "r1")
    assert r1["boundary_refs"] == 3
    assert r1["held_for_blocks"] == [1, 2]
    # 两个消费块各自释放一次引用（记录在案）。
    releases = [
        e for e in result.block_events if e.get("phase") == "release_boundary_ref"
    ]
    r1_rel = [e for e in releases if e["boundary"] == "r1"]
    assert len(r1_rel) == 2


def test_shared_gradient_accumulation_matches_baseline(cross_graph) -> None:
    # 两个分支对共享祖先 r1/l1/x 的梯度贡献必须被正确累加。
    ck = _run(cross_graph, (2, 5))
    base = _baseline(cross_graph)
    for nid in base.param_grads:
        for p in base.param_grads[nid]:
            np.testing.assert_allclose(
                ck.param_grads[nid][p], base.param_grads[nid][p], rtol=0, atol=0
            )
    np.testing.assert_allclose(ck.input_grads["x"], base.input_grads["x"], rtol=0, atol=0)


# --------------------------------------------------------------------- #
# 梯度一致性（固定 dropout 夹具 + 分支图）
# --------------------------------------------------------------------- #
def test_gradients_identical_across_all_legal_plans(branch_graph) -> None:
    base = _baseline(branch_graph)
    for plan, prof in legal_plans(branch_graph):
        res = execute(
            TrainState.synthetic(branch_graph, seed=99),
            plan,
            predicted_peak=prof.peak_elements,
        )
        for nid in base.param_grads:
            for p in base.param_grads[nid]:
                np.testing.assert_array_equal(
                    res.param_grads[nid][p], base.param_grads[nid][p]
                )
        np.testing.assert_array_equal(res.input_grads["x"], base.input_grads["x"])
        np.testing.assert_array_equal(res.outputs["l2"], base.outputs["l2"])


def test_outputs_and_grads_identical_chain(chain_graph) -> None:
    ck = _run(chain_graph, (2,), seed=42)
    base = _baseline(chain_graph, seed=42)
    np.testing.assert_array_equal(ck.outputs["l2"], base.outputs["l2"])
    np.testing.assert_allclose(ck.input_grads["x"], base.input_grads["x"], atol=0)


def test_injected_forward_fault_is_compute_failure(chain_graph) -> None:
    plan, prof = baseline_plan(chain_graph)
    with pytest.raises(ComputeFailureError) as exc:
        execute(
            TrainState.synthetic(chain_graph, seed=99),
            plan,
            predicted_peak=prof.peak_elements,
            faults=frozenset({"r1"}),
        )
    assert exc.value.category == "compute_failure"
    assert exc.value.details["node"] == "r1"


def test_forward_advance_conflict(chain_graph) -> None:
    # 同一 TrainState 不允许推进两次前向（状态冲突而非静默覆盖 RNG）。
    plan, prof = baseline_plan(chain_graph)
    state = TrainState.synthetic(chain_graph, seed=99)
    execute(state, plan, predicted_peak=prof.peak_elements)
    with pytest.raises(StateConflictError) as exc:
        execute(state, plan, predicted_peak=prof.peak_elements)
    assert exc.value.category == "state_conflict"
