"""独立数值验证测试：有限差分黄金参考与前向独立实现对账。

有限差分与独立前向都不导入 engine/planner，因此参考答案不是被测核心自己生成的。
"""

from __future__ import annotations

import numpy as np
from recomp_scheduler.engine import execute
from recomp_scheduler.memory import simulate
from recomp_scheduler.planner import make_plan
from recomp_scheduler.state import TrainState
from recomp_scheduler.verification import (
    finite_difference_check,
    reference_forward,
    _draw_masks,
)


def _fd(graph, cuts, seed, **fd_kw):
    plan = make_plan(cuts, len(graph.order))
    plan.validate(graph)
    state = TrainState.synthetic(graph, seed=seed)
    result = execute(state, plan, predicted_peak=simulate(graph, plan).peak_elements)
    report = finite_difference_check(
        graph,
        state.inputs,
        state.params,
        state.grad_outputs,
        seed,
        result.input_grads,
        result.param_grads,
        **fd_kw,
    )
    return state, result, report


def test_finite_difference_chain_checkpointed(chain_graph) -> None:
    state, result, report = _fd(chain_graph, (2,), seed=42)
    assert report.passed, report.per_component_max_err
    # 具体上界：解析梯度与中心差分吻合到 1e-6 以内（实测 ~1e-10）。
    assert report.max_abs_err < 1e-6
    assert report.n_components > 0
    # 每个可微分量（输入 + 全部参数）都被独立核对。
    assert set(report.per_component_max_err) == {
        "input:x",
        "param:l1:W",
        "param:l1:b",
        "param:l2:W",
        "param:l2:b",
    }


def test_finite_difference_branch_with_shared_node(branch_graph) -> None:
    state, result, report = _fd(branch_graph, (3,), seed=99)
    assert report.passed, report.per_component_max_err
    assert report.max_abs_err < 1e-6


def test_finite_difference_cross_block_replay(cross_graph) -> None:
    # 含块内 dropout 重放的方案：重放正确性最终由有限差分兜底。
    state, result, report = _fd(cross_graph, (2, 5), seed=99)
    assert report.passed, report.per_component_max_err
    assert report.max_abs_err < 1e-6
    assert result.effects_replay_verified == 1


def test_reference_forward_matches_engine_outputs(chain_graph) -> None:
    plan = make_plan((2,), len(chain_graph.order))
    state = TrainState.synthetic(chain_graph, seed=42)
    result = execute(state, plan, predicted_peak=simulate(graph=chain_graph, plan=plan).peak_elements)
    masks = _draw_masks(chain_graph, 42)
    ref = reference_forward(chain_graph, state.inputs, state.params, masks)
    np.testing.assert_array_equal(ref["l2"], result.outputs["l2"])


def test_finite_difference_detects_wrong_gradient(chain_graph) -> None:
    """反向健全性：把引擎梯度污染后，FD 必须判失败——证明它有鉴别力。"""
    plan = make_plan((), len(chain_graph.order))
    state = TrainState.synthetic(chain_graph, seed=42)
    result = execute(state, plan, predicted_peak=simulate(chain_graph, plan).peak_elements)
    # 明确污染一个参数梯度。
    result.param_grads["l1"]["W"][0, 0] += 1.0
    report = finite_difference_check(
        chain_graph,
        state.inputs,
        state.params,
        state.grad_outputs,
        42,
        result.input_grads,
        result.param_grads,
        tolerance=1e-6,
    )
    assert not report.passed
    assert report.worst_component == "param:l1:W"
    assert report.max_abs_err > 0.5
