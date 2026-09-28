"""算子原语库。

每个算子是一个 :class:`OpSpec`，声明四类显式契约：

1. ``out_shape``         由输入形状/属性推断输出形状（形状错误在图构建期暴露）；
2. ``saved_elements``    除输出激活外、需要为反向保留的缓冲区大小（如 mask）；
3. ``workspace_*``       前向/反向期间的**临时工作区**大小（用后即释放）；
4. ``forward/backward``  纯数值实现；随机性只能通过 :class:`OpEnv` 的 RNG 获取。

随机算子（dropout）不得自行创建随机源，也不得直接写外部日志——
所有"外部副作用"（随机掩码发放）都必须经过 :class:`OpEnv`，
引擎在重放时会切换为校验模式而非重复发放。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from .errors import ComputeFailureError, GraphValidationError, StateConflictError

# 前向/反向签名
ForwardFn = Callable[["OpContext", list[np.ndarray]], np.ndarray]
BackwardFn = Callable[
    ["OpContext", np.ndarray, list[np.ndarray], np.ndarray, list[np.ndarray]],
    list[np.ndarray],
]


@dataclass(frozen=True)
class ParamSpec:
    name: str
    shape: tuple[int, ...]


@dataclass(frozen=True)
class OpSpec:
    name: str
    n_inputs: int  # -1 表示由属性/输入列表在形状推断时决定
    has_params: bool
    needs_rng: bool
    out_shape_fn: Callable[[dict[str, Any], list[tuple[int, ...]]], tuple[int, ...]]
    param_specs_fn: Callable[
        [dict[str, Any], list[tuple[int, ...]], tuple[int, ...]], list[ParamSpec]
    ]
    forward_fn: ForwardFn
    backward_fn: BackwardFn
    saved_elements_fn: Callable[[dict[str, Any], tuple[int, ...]], int] = lambda a, s: 0
    workspace_fn: Callable[[dict[str, Any], tuple[int, ...]], int] = lambda a, s: 0
    backward_workspace_fn: Callable[[dict[str, Any], tuple[int, ...]], int] = (
        lambda a, s: 0
    )

    def out_shape(self, attrs: dict[str, Any], in_shapes) -> tuple[int, ...]:
        return tuple(int(x) for x in self.out_shape_fn(attrs, list(in_shapes)))

    def param_specs(self, attrs, in_shapes, out_shape) -> list[ParamSpec]:
        return self.param_specs_fn(attrs, list(in_shapes), tuple(out_shape))

    def saved_elements(self, attrs, out_shape: tuple[int, ...]) -> int:
        return int(self.saved_elements_fn(attrs, tuple(out_shape)))

    def workspace(self, attrs, out_shape: tuple[int, ...]) -> int:
        return int(self.workspace_fn(attrs, tuple(out_shape)))

    def backward_workspace(self, attrs, out_shape: tuple[int, ...]) -> int:
        return int(self.backward_workspace_fn(attrs, tuple(out_shape)))


@dataclass
class OpEnv:
    """算子执行环境（由引擎注入，算子不自行构造）。

    mode == "forward"  ：RNG 正常发放，外部事件写入日志；
    mode == "recompute"：RNG 从块边界快照重放，外部事件只做逐字节校验，
                         绝不重复发放（这是"避免重复外部副作用"的保证点）。
    """

    rng: np.random.Generator
    mode: str = "forward"
    effects: "EffectLog | None" = None

    def emit_mask(self, node_id: str, scaled_mask: np.ndarray) -> None:
        """登记/校验一次随机掩码发放。"""
        if self.effects is not None:
            payload = scaled_mask.tobytes()
            self.effects.observe(
                kind="dropout_mask", node=node_id, payload=payload, mode=self.mode
            )


@dataclass
class OpContext:
    node_id: str
    attrs: dict[str, Any]
    env: OpEnv
    saved: list[np.ndarray]  # forward 写入，backward 只读
    params: list[np.ndarray]  # 可训练参数（无参数算子为空）
    workspace: "np.ndarray | None" = None  # 临时工作区（引擎在调用期分配）

    @property
    def rng(self) -> np.random.Generator:
        return self.env.rng


class EffectLog:
    """外部副作用账本：前向发放一次，重放按键只校验。

    随机副作用以**节点 id 为键**（每个 dropout 节点前向恰好发放一次掩码）：

    - 前向阶段（``mode="forward"``）：记录逐字节 payload；重复发放属于
      状态冲突（:class:`StateConflictError`）；
    - 重放阶段（``mode="recompute"``）：按节点键与前向记录逐字节比对，
      计入 :attr:`replay_verified`；多放、放错节点、掩码不一致都抛
      :class:`ComputeFailureError`；
    - 重放绝不新增副作用，因此"避免重复外部副作用"由类型与账本共同保证。

    注意：块边界节点与最后一块中的随机节点**不重放**（其 saved 掩码前向
    保留），因此 ``replay_verified`` 只覆盖真正被重算的节点。
    """

    def __init__(self) -> None:
        self.emitted: dict[str, dict[str, Any]] = {}
        self.forward_order: list[str] = []
        self.replay_verified = 0

    def observe(self, *, kind: str, node: str, payload: bytes, mode: str) -> None:
        if mode == "recompute":
            expected = self.emitted.get(node)
            if expected is None:
                raise ComputeFailureError(
                    "replay produced an effect with no forward counterpart",
                    kind=kind,
                    node=node,
                )
            if expected.get("verified"):
                raise StateConflictError(
                    "same RNG effect verified twice during replay", node=node
                )
            if expected["kind"] != kind:
                raise ComputeFailureError(
                    "replayed effect kind mismatch",
                    node=node,
                    expected_kind=expected["kind"],
                    actual_kind=kind,
                )
            if expected["payload"] != payload:
                raise ComputeFailureError(
                    "replayed RNG draw differs from forward draw",
                    kind=kind,
                    node=node,
                )
            expected["verified"] = True
            self.replay_verified += 1
            return
        if mode != "forward":
            raise StateConflictError("unknown op-env mode", mode=mode)
        if node in self.emitted:
            raise StateConflictError(
                "duplicate forward emission of the same external effect", node=node
            )
        self.emitted[node] = {
            "kind": kind,
            "node": node,
            "payload": payload,
            "verified": False,
        }
        self.forward_order.append(node)

    def unverified(self) -> list[str]:
        """前向发放但未被任何重放校验的节点（边界/末块随机节点属预期）。"""
        return [n for n, rec in self.emitted.items() if not rec["verified"]]


# --------------------------------------------------------------------- #
# 形状推断
# --------------------------------------------------------------------- #
def _linear_shape(attrs, in_shapes):
    out_features = int(attrs["out_features"])
    return (*in_shapes[0][:-1], out_features)


def _same_shape(attrs, in_shapes):
    return in_shapes[0]


def _input_shape(attrs, in_shapes):
    return tuple(int(x) for x in attrs["shape"])


# --------------------------------------------------------------------- #
# 前向/反向实现
# --------------------------------------------------------------------- #
def _linear_params(attrs, in_shapes, out_shape):
    in_features = int(in_shapes[0][-1])
    out_features = int(out_shape[-1])
    return [
        ParamSpec("W", (in_features, out_features)),
        ParamSpec("b", (out_features,)),
    ]


def _linear_forward(ctx, inputs):
    (x,) = inputs
    W, b = ctx.params
    # 显式临时工作区：模拟 GEMM 调度缓冲（大小 = 输出元素数）。
    if ctx.workspace is not None:
        ctx.workspace[:] = 0.0
    return x @ W + b


def _linear_backward(ctx, grad_out, inputs, out, saved):
    (x,) = inputs
    W, _b = ctx.params
    g_x = grad_out @ W.T
    g_W = x.reshape(-1, x.shape[-1]).T @ grad_out.reshape(-1, grad_out.shape[-1])
    g_b = grad_out.reshape(-1, grad_out.shape[-1]).sum(axis=0)
    return [g_x, g_W, g_b]


def _relu_forward(ctx, inputs):
    out = np.maximum(inputs[0], 0.0)
    # 反向需要的正/负掩码，作为 saved 缓冲保存（计入内存账）。
    ctx.saved.append((inputs[0] > 0).astype(np.float64))
    return out


def _relu_backward(ctx, grad_out, inputs, out, saved):
    (mask,) = saved
    return [grad_out * mask]


def _tanh_forward(ctx, inputs):
    return np.tanh(inputs[0])


def _tanh_backward(ctx, grad_out, inputs, out, saved):
    return [grad_out * (1.0 - out**2)]


def _add_forward(ctx, inputs):
    return inputs[0] + inputs[1]


def _add_backward(ctx, grad_out, inputs, out, saved):
    return [grad_out, grad_out]


def _mul_forward(ctx, inputs):
    return inputs[0] * inputs[1]


def _mul_backward(ctx, grad_out, inputs, out, saved):
    return [grad_out * inputs[1], grad_out * inputs[0]]


def _dropout_forward(ctx, inputs):
    p = float(ctx.attrs["keep_prob"])
    if not 0.0 < p <= 1.0:
        raise GraphValidationError(
            "dropout keep_prob must be in (0, 1]", keep_prob=p
        )
    draws = ctx.rng.random(inputs[0].shape, dtype=np.float64)
    scaled_mask = (draws < p).astype(np.float64) / p
    ctx.env.emit_mask(ctx.node_id, scaled_mask)
    ctx.saved.append(scaled_mask)
    return inputs[0] * scaled_mask


def _dropout_backward(ctx, grad_out, inputs, out, saved):
    (scaled_mask,) = saved
    return [grad_out * scaled_mask]


REGISTRY: dict[str, OpSpec] = {
    "input": OpSpec(
        name="input",
        n_inputs=0,
        has_params=False,
        needs_rng=False,
        out_shape_fn=_input_shape,
        param_specs_fn=lambda a, i, o: [],
        forward_fn=lambda ctx, inputs: np.empty(0),
        backward_fn=lambda ctx, g, i, o, s: [],
    ),
    "linear": OpSpec(
        name="linear",
        n_inputs=1,
        has_params=True,
        needs_rng=False,
        out_shape_fn=_linear_shape,
        param_specs_fn=_linear_params,
        forward_fn=_linear_forward,
        backward_fn=_linear_backward,
        workspace_fn=lambda a, s: int(np.prod(s)),
        backward_workspace_fn=lambda a, s: int(np.prod(s)),
    ),
    "relu": OpSpec(
        name="relu",
        n_inputs=1,
        has_params=False,
        needs_rng=False,
        out_shape_fn=_same_shape,
        param_specs_fn=lambda a, i, o: [],
        forward_fn=_relu_forward,
        backward_fn=_relu_backward,
        saved_elements_fn=lambda a, s: int(np.prod(s)),
    ),
    "tanh": OpSpec(
        name="tanh",
        n_inputs=1,
        has_params=False,
        needs_rng=False,
        out_shape_fn=_same_shape,
        param_specs_fn=lambda a, i, o: [],
        forward_fn=_tanh_forward,
        backward_fn=_tanh_backward,
    ),
    "add": OpSpec(
        name="add",
        n_inputs=2,
        has_params=False,
        needs_rng=False,
        out_shape_fn=_same_shape,
        param_specs_fn=lambda a, i, o: [],
        forward_fn=_add_forward,
        backward_fn=_add_backward,
    ),
    "mul": OpSpec(
        name="mul",
        n_inputs=2,
        has_params=False,
        needs_rng=False,
        out_shape_fn=_same_shape,
        param_specs_fn=lambda a, i, o: [],
        forward_fn=_mul_forward,
        backward_fn=_mul_backward,
    ),
    "dropout": OpSpec(
        name="dropout",
        n_inputs=1,
        has_params=False,
        needs_rng=True,
        out_shape_fn=_same_shape,
        param_specs_fn=lambda a, i, o: [],
        forward_fn=_dropout_forward,
        backward_fn=_dropout_backward,
        saved_elements_fn=lambda a, s: int(np.prod(s)),
    ),
}


def get_spec(op: str) -> OpSpec:
    try:
        return REGISTRY[op]
    except KeyError:
        raise GraphValidationError("unknown op", op=op, known=sorted(REGISTRY))


def finite_guard(result: np.ndarray, node_id: str, phase: str) -> None:
    if not np.isfinite(result).all():
        raise ComputeFailureError(
            f"non-finite op output at {node_id!r} ({phase})",
            node=node_id,
            phase=phase,
        )


__all__ = [
    "OpSpec",
    "OpEnv",
    "OpContext",
    "EffectLog",
    "REGISTRY",
    "get_spec",
    "finite_guard",
    "Tensor",
]
