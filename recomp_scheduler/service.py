"""服务编排层：把图构建/规划/执行/验证/日志串成一次请求的完整用例。

:func:`plan_and_run` 是主要入口：构建图 -> 预算内选方案 -> 合成夹具 ->
执行（RNG 重放）-> 梯度一致性（基线对照，可选独立有限差分黄金参考）->
写运行日志。任何已知错误都以 :class:`~recomp_scheduler.errors.RecompError`
子类返回，API 层据此映射 HTTP 状态码与错误信封。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import DEFAULT_CONFIG
from .engine import RunResult, execute
from .graph import Graph, build_graph
from .journal import Journal, new_run_id
from .planner import Plan, baseline_plan, choose_plan, legal_plans
from .state import TrainState
from .verification import finite_difference_check, reference_forward, _draw_masks


def graph_fingerprint(raw_nodes: list[dict[str, Any]], outputs: list[str] | None) -> str:
    """对图声明做稳定哈希（用于日志关联与复现）。"""
    payload = json.dumps(
        {"nodes": raw_nodes, "outputs": outputs}, sort_keys=True, default=str
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


@dataclass
class RunSummary:
    run_id: str
    graph: Graph
    plan: Plan
    profile: Any
    result: RunResult
    candidates_evaluated: int
    candidates_legal: int
    rejected_by_budget: int
    grad_check: dict[str, Any]
    reason: str


def _max_grad_diff(a: RunResult, b: RunResult) -> float:
    worst = 0.0
    for nid in a.param_grads:
        for p in a.param_grads[nid]:
            worst = max(
                worst, float(np.max(np.abs(a.param_grads[nid][p] - b.param_grads[nid][p])))
            )
    for v in a.input_grads:
        worst = max(worst, float(np.max(np.abs(a.input_grads[v] - b.input_grads[v]))))
    return worst


def plan_and_run(
    raw_nodes: list[dict[str, Any]],
    *,
    outputs: list[str] | None = None,
    budget_elements: int,
    seed: int = 1234,
    run_finite_difference: bool = False,
    verify_gradients: bool = True,
    journal: Journal | None = None,
    config=DEFAULT_CONFIG,
) -> RunSummary:
    """完整用例：在预算内选方案并执行、校验、记录。

    ``verify_gradients=False`` 时跳过基线梯度对照与有限差分（仅规划+执行+
    峰值对账），``grad_check`` 为 None；该开关同时被 API 的同名字段控制。
    """
    journal = journal or Journal(config.journal_dir)
    fp = graph_fingerprint(raw_nodes, outputs)
    graph = build_graph(raw_nodes, outputs=outputs)  # input_error 直接上抛

    choice = choose_plan(graph, budget_elements)  # resource_exhausted 直接上抛
    plan = choice.plan
    profile = choice.profile

    # ---- 执行所选方案（固定夹具 + 固定 dropout 种子） ----
    state = TrainState.synthetic(graph, seed=seed)
    result = execute(
        state,
        plan,
        predicted_peak=profile.peak_elements,
        budget_elements=budget_elements,
    )

    if not verify_gradients:
        grad_check: dict[str, Any] | None = None
    else:
        # ---- 梯度一致性：基线（不检查点）必须给出相同梯度 ----
        base_plan, base_profile = baseline_plan(graph)
        base_state = TrainState.synthetic(graph, seed=seed)
        baseline = execute(
            base_state, base_plan, predicted_peak=base_profile.peak_elements
        )
        max_diff = _max_grad_diff(baseline, result)
        out_diff = max(
            float(np.max(np.abs(baseline.outputs[o] - result.outputs[o])))
            for o in graph.outputs
        )
        grad_check = {
            "baseline_peak": baseline.runtime_peak,
            "checkpoint_peak": result.runtime_peak,
            "max_grad_abs_diff_vs_baseline": max_diff,
            "max_output_abs_diff": out_diff,
            "tolerance": config.grad_tol,
            "passed": bool(
                max_diff <= config.grad_tol and out_diff <= config.grad_tol
            ),
        }

        if not grad_check["passed"]:
            # 基线与检查点梯度不一致只能是重放/记账实现缺陷。
            from .errors import ComputeFailureError

            raise ComputeFailureError(
                "checkpointed gradients disagree with baseline",
                max_grad_abs_diff=max_diff,
                max_output_abs_diff=out_diff,
                tolerance=config.grad_tol,
            )

        # ---- 可选：独立有限差分黄金参考 ----
        if run_finite_difference:
            report = finite_difference_check(
                graph,
                state.inputs,
                state.params,
                state.grad_outputs,
                seed,
                result.input_grads,
                result.param_grads,
                epsilon=config.finite_diff_eps,
                tolerance=config.grad_tol,
            )
            fd = report.summary()
            if not report.passed:
                from .errors import ComputeFailureError

                raise ComputeFailureError(
                    "finite-difference gradient check failed", **fd
                )
            grad_check["finite_difference"] = fd

        # ---- 前向值再用独立参考实现对账一次 ----
        masks = _draw_masks(graph, seed)
        ref_values = reference_forward(graph, state.inputs, state.params, masks)
        ref_out_diff = max(
            float(np.max(np.abs(ref_values[o] - result.outputs[o])))
            for o in graph.outputs
        )
        grad_check["reference_output_abs_diff"] = ref_out_diff

    run_id = new_run_id("step")
    if verify_gradients:
        grad_tail = f"grad match vs baseline max-abs-diff {max_diff:.3e}"
    else:
        grad_tail = "gradient verification skipped"
    reason = (
        f"selected {plan.block_count}-block plan (cuts at {list(plan.boundary_positions)}); "
        f"predicted peak {profile.peak_elements} == runtime peak {result.runtime_peak}; "
        f"extra recompute {profile.recompute_flops} flops "
        f"({profile.recompute_ratio:.2%} of base fwd+bwd); "
        f"effects emitted={result.effects_emitted} replay-verified={result.effects_replay_verified}; "
        f"{grad_tail}"
    )
    journal.record_plan(
        graph_fingerprint=fp,
        budget=budget_elements,
        plan_dict=plan.to_dict(graph),
        profile=profile.summary(),
        candidates_evaluated=choice.candidates_evaluated,
        reason=reason,
    )
    journal.record_execute(
        graph_fingerprint=fp,
        run_id=run_id,
        seed=seed,
        plan_dict=plan.to_dict(graph),
        predicted_peak=profile.peak_elements,
        runtime_peak=result.runtime_peak,
        peak_match=result.peak_match(),
        effects_emitted=result.effects_emitted,
        effects_replayed=result.effects_replay_verified,
        grad_norms=result.grad_norms(),
        snapshot_balance=result.rng_snapshot_balance,
        reason=reason,
    )

    return RunSummary(
        run_id=run_id,
        graph=graph,
        plan=plan,
        profile=profile,
        result=result,
        candidates_evaluated=choice.candidates_evaluated,
        candidates_legal=choice.candidates_legal,
        rejected_by_budget=choice.rejected_by_budget,
        grad_check=grad_check,
        reason=reason,
    )


def enumerate_plans(
    raw_nodes: list[dict[str, Any]],
    *,
    outputs: list[str] | None = None,
) -> dict[str, Any]:
    """穷举全部合法方案并附静态画像（供小图穷举对照测试/接口）。"""
    graph = build_graph(raw_nodes, outputs=outputs)
    scored = legal_plans(graph)
    candidates = [
        {"plan": p.to_dict(graph), "profile": prof.summary()}
        for p, prof in scored
    ]
    base_plan, base_prof = baseline_plan(graph)
    min_peak = min(prof.peak_elements for _, prof in scored)
    at_min = [
        prof.recompute_flops
        for _, prof in scored
        if prof.peak_elements == min_peak
    ]
    return {
        "graph": graph,
        "candidates": candidates,
        "baseline": {
            "plan": base_plan.to_dict(graph),
            "profile": base_prof.summary(),
        },
        "min_peak": min_peak,
        "min_recompute_at_min_peak": min(at_min),
        "legal_candidate_count": len(candidates),
    }
