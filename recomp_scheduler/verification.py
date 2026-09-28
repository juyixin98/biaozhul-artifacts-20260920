"""独立数值验证。

这里给出**不依赖 planner/engine/arena** 的参考答案，用于回答
"被测核心自己给自己判卷"的问题：

- :func:`reference_forward` 用独立的 NumPy 实现重算前向与损失，并按合同
  （``np.random.default_rng(seed)`` 沿拓扑序为每个 dropout 节点抽一次掩码）
  独立生成固定 dropout 掩码；
- :func:`finite_difference_check` 对**参数和输入逐标量**做中心差分，
  得到梯度的黄金参考，与引擎产出的梯度逐元素比较。

它只依赖图的静态声明与 numpy；不导入 :mod:`recomp_scheduler.engine`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import ComputeFailureError, GraphValidationError
from .graph import Graph


@dataclass(frozen=True)
class FdReport:
    max_abs_err: float
    tolerance: float
    n_components: int
    worst_component: str
    passed: bool
    per_component_max_err: dict[str, float]

    def summary(self) -> dict[str, Any]:
        return {
            "max_abs_err": self.max_abs_err,
            "tolerance": self.tolerance,
            "n_components": self.n_components,
            "worst_component": self.worst_component,
            "passed": self.passed,
        }


def _draw_masks(graph: Graph, seed: int, keep_prob_override: dict[str, float] | None = None):
    """按 RNG 合同独立抽取固定 dropout 掩码（拓扑序，每节点一次）。"""
    rng = np.random.default_rng(seed)
    masks: dict[str, np.ndarray] = {}
    for v in graph.order:
        node = graph.node(v)
        if node.op != "dropout":
            continue
        p = (
            keep_prob_override[v]
            if keep_prob_override and v in keep_prob_override
            else float(node.attrs["keep_prob"])
        )
        draws = rng.random(node.shape, dtype=np.float64)
        masks[v] = (draws < p).astype(np.float64) / p
    return masks


def reference_forward(
    graph: Graph,
    feeds: dict[str, np.ndarray],
    params: dict[str, dict[str, np.ndarray]],
    masks: dict[str, np.ndarray],
) -> dict[str, np.ndarray]:
    """完全独立的前向求值（无检查点、无记账、无 RNG 对象传递）。"""
    values: dict[str, np.ndarray] = {}
    for v in graph.order:
        node = graph.node(v)
        if node.op == "input":
            values[v] = np.asarray(feeds[v], dtype=np.float64)
        elif node.op == "linear":
            W = params[v]["W"]
            b = params[v]["b"]
            values[v] = values[node.inputs[0]] @ W + b
        elif node.op == "relu":
            values[v] = np.maximum(values[node.inputs[0]], 0.0)
        elif node.op == "tanh":
            values[v] = np.tanh(values[node.inputs[0]])
        elif node.op == "add":
            values[v] = values[node.inputs[0]] + values[node.inputs[1]]
        elif node.op == "mul":
            values[v] = values[node.inputs[0]] * values[node.inputs[1]]
        elif node.op == "dropout":
            values[v] = values[node.inputs[0]] * masks[v]
        else:  # pragma: no cover - 注册表扩展时的防御
            raise GraphValidationError("reference forward missing op", op=node.op)
    return values


def scalar_loss(
    graph: Graph,
    values: dict[str, np.ndarray],
    grad_outputs: dict[str, np.ndarray],
) -> float:
    """把多个输出按给定上游梯度点积为标量（与反传目标一致）。"""
    total = 0.0
    for o in graph.outputs:
        total += float(np.sum(values[o] * grad_outputs[o]))
    return total


def _flatten_targets(
    graph: Graph,
    feeds: dict[str, np.ndarray],
    params: dict[str, dict[str, np.ndarray]],
):
    """把所有输入与参数展开为可逐标量扰动的片段列表。"""
    pieces: list[tuple[str, np.ndarray]] = []
    for v in graph.inputs:
        pieces.append((f"input:{v}", feeds[v]))
    for nid in graph.order:
        for spec in graph.node(nid).params:
            pieces.append((f"param:{nid}:{spec.name}", params[nid][spec.name]))
    return pieces


def finite_difference_check(
    graph: Graph,
    feeds: dict[str, np.ndarray],
    params: dict[str, dict[str, np.ndarray]],
    grad_outputs: dict[str, np.ndarray],
    seed: int,
    engine_input_grads: dict[str, np.ndarray],
    engine_param_grads: dict[str, dict[str, np.ndarray]],
    *,
    epsilon: float = 1e-5,
    tolerance: float = 1e-6,
) -> FdReport:
    """对每个可微标量做中心差分，与引擎梯度逐元素比较。

    为控制小规模合成夹具下的成本，逐分量比较；图规模在测试中固定且很小。
    dropout 掩码在整个差分过程中保持固定（合同的可重放性使这成为合法操作）。
    """
    masks = _draw_masks(graph, seed)

    def loss_with(feeds_, params_):
        values = reference_forward(graph, feeds_, params_, masks)
        return scalar_loss(graph, values, grad_outputs)

    targets: list[tuple[str, np.ndarray, str]] = []
    for v in graph.inputs:
        targets.append((v, feeds[v], "input"))
    for nid in graph.order:
        for spec in graph.node(nid).params:
            targets.append((nid, params[nid][spec.name], f"param:{spec.name}"))

    per_component: dict[str, float] = {}
    worst = 0.0
    worst_name = ""
    n_components = 0

    for nid, arr, kind in targets:
        grad_expected = np.zeros_like(arr)
        it = np.ndindex(arr.shape)
        for idx in it:
            original = float(arr[idx])
            arr[idx] = original + epsilon
            plus = loss_with(feeds, params)
            arr[idx] = original - epsilon
            minus = loss_with(feeds, params)
            arr[idx] = original
            grad_expected[idx] = (plus - minus) / (2.0 * epsilon)

        if kind == "input":
            actual = engine_input_grads[nid]
            comp_name = f"input:{nid}"
        else:
            pname = kind.split(":", 1)[1]
            actual = engine_param_grads[nid][pname]
            comp_name = f"param:{nid}:{pname}"

        if actual.shape != grad_expected.shape:
            raise ComputeFailureError(
                "gradient shape mismatch in FD check",
                component=comp_name,
                expected=grad_expected.shape,
                actual=actual.shape,
            )
        err = float(np.max(np.abs(actual - grad_expected)))
        per_component[comp_name] = err
        n_components += int(arr.size)
        if err > worst:
            worst, worst_name = err, comp_name

    passed = worst <= tolerance
    return FdReport(
        max_abs_err=worst,
        tolerance=tolerance,
        n_components=n_components,
        worst_component=worst_name,
        passed=passed,
        per_component_max_err=per_component,
    )
